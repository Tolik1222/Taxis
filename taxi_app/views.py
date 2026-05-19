import os
import time
import hmac
import hashlib
import math
import random
from urllib.parse import quote_plus
import stripe
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Order, UserProfile, PromoCode, SupportTicket, SupportMessage
from .utils import TIER_RULES, get_route_info, calculate_price_details, DEFAULT_CURRENCY, geocode_city


def _get_profile(user):
    profile = UserProfile.objects.filter(user=user).first()
    if profile is None:
        profile = UserProfile.objects.create(user=user, role='passenger')
    return profile


def _is_support_or_admin(user):
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return _get_profile(user).role == "support"


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _ensure_bot_drivers(count=8):
    bots = []
    base_lat = 50.4501
    base_lon = 30.5234
    # Боти мають стабільні логіни, щоб не плодити нових користувачів на кожному запиті.
    for i in range(1, count + 1):
        username = f"bot_driver_{i}"
        user, _ = User.objects.get_or_create(
            username=username,
            defaults={"first_name": f"Бот {i}"},
        )
        profile, _ = UserProfile.objects.get_or_create(
            user=user,
            defaults={"role": "driver"},
        )
        changed_fields = []
        if profile.role != "driver":
            profile.role = "driver"
            changed_fields.append("role")
        if not profile.is_online:
            profile.is_online = True
            changed_fields.append("is_online")
        if not profile.is_bot_driver:
            profile.is_bot_driver = True
            changed_fields.append("is_bot_driver")
        if profile.lat is None or profile.lon is None:
            profile.lat = base_lat + random.uniform(-0.03, 0.03)
            profile.lon = base_lon + random.uniform(-0.03, 0.03)
            changed_fields.extend(["lat", "lon"])
        if not profile.car_make:
            profile.car_make = "BotCar"
            changed_fields.append("car_make")
        if not profile.car_model:
            profile.car_model = f"M{i}"
            changed_fields.append("car_model")
        if not profile.car_plate:
            profile.car_plate = f"BOT{i:03d}"
            changed_fields.append("car_plate")
        if changed_fields:
            profile.save(update_fields=changed_fields)
        bots.append(profile)
    return bots


def _move_bot_driver(profile):
    # Тут простий "блукаючий" рух у межах міста; цього достатньо для MVP-демо на мапі.
    if profile.lat is None or profile.lon is None:
        profile.lat = 50.4501 + random.uniform(-0.02, 0.02)
        profile.lon = 30.5234 + random.uniform(-0.02, 0.02)
    profile.lat += random.uniform(-0.0015, 0.0015)
    profile.lon += random.uniform(-0.0018, 0.0018)
    profile.save(update_fields=["lat", "lon"])


def _create_stripe_checkout(order, request, amount, payment_type="deposit"):
    api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not api_key:
        return {"error": "Stripe не налаштований (STRIPE_SECRET_KEY)."}

    public_base = os.getenv("STRIPE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base:
        success_url = (
            f"{public_base}{reverse('index')}?paid=1&order_id={order.id}"
            "&session_id={CHECKOUT_SESSION_ID}"
        )
        cancel_url = f"{public_base}{reverse('index')}?paid=0&order_id={order.id}"
    else:
        success_url = (
            request.build_absolute_uri(reverse("index"))
            + f"?paid=1&order_id={order.id}&session_id={{CHECKOUT_SESSION_ID}}"
        )
        cancel_url = request.build_absolute_uri(reverse("index")) + f"?paid=0&order_id={order.id}"
    currency = (os.getenv("STRIPE_CURRENCY", "usd") or "usd").strip().lower()
    amount_minor = int(float(amount) * 100)
    if amount_minor <= 0:
        return {"error": "Некоректна сума для Stripe checkout."}

    try:
        stripe.api_key = api_key
        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(order.id),
            metadata={
                "order_id": str(order.id),
                "payment_method": "card",
                "payment_type": payment_type,
            },
            line_items=[
                {
                    "price_data": {
                        "currency": currency,
                        "product_data": {
                            "name": f"Taxi Pro #{order.id} ({'Передплата' if payment_type == 'deposit' else 'Доплата'})",
                            "description": f"Поїздка класу {order.service_tier} (очікування: {order.waiting_seconds}с)",
                        },
                        "unit_amount": amount_minor,
                    },
                    "quantity": 1,
                }
            ],
        )
    except stripe.error.StripeError as exc:
        msg = getattr(exc, "user_message", None) or str(exc)
        return {"error": msg or "Stripe повернув помилку."}
    except Exception:
        return {"error": "Не вдалося звернутися до Stripe."}

    if not session.url:
        return {"error": "Stripe не повернув checkout URL."}
    return {"checkout_url": session.url, "transaction_id": session.id}


@ensure_csrf_cookie
def index_page(request):
    paid = request.GET.get('paid')
    order_id = request.GET.get('order_id')
    session_id = request.GET.get('session_id')

    if paid == '1' and order_id and session_id:
        try:
            stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
            session = stripe.checkout.Session.retrieve(session_id)
            if session.payment_status == "paid":
                order = Order.objects.filter(pk=order_id).first()
                if order:
                    payment_type = session.metadata.get("payment_type", "deposit") if getattr(session, "metadata", None) else "deposit"
                    if payment_type == "deposit" and order.payment_status in ["pending_deposit", "pending"]:
                        order.payment_status = "deposit_paid"
                        if order.status == "new":
                            order.status = "searching"
                        order.paddle_transaction_id = session.id[:64]
                        order.save(update_fields=["payment_status", "status", "paddle_transaction_id"])
                    elif payment_type == "remainder" and order.payment_status == "pending_remainder":
                        order.payment_status = "paid"
                        order.paddle_transaction_id = session.id[:64]
                        order.save(update_fields=["payment_status", "paddle_transaction_id"])
        except Exception as e:
            print("Stripe sync check error:", e)

    profile = None
    if request.user.is_authenticated:
        profile = _get_profile(request.user)
    needs_role_gate = request.session.get('show_role_confirmation', False)
    telegram_bot_username = os.getenv('TELEGRAM_BOT_USERNAME', 'TolikTaxi1_Bot')
    telegram_redirect_uri = request.build_absolute_uri(reverse('telegram_callback'))
    telegram_token_present = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
    return render(
        request,
        'taxi_app/index.html',
        {
            'profile': profile,
            'needs_role_gate': needs_role_gate,
            'telegram_bot_username': telegram_bot_username,
            'telegram_redirect_uri': telegram_redirect_uri,
            'telegram_token_present': telegram_token_present,
            'telegram_login_hint': (
                not request.user.is_authenticated
                and request.GET.get('tg_err') == '1'
            ),
            'telegram_login_reason': request.GET.get('tg_reason', ''),
        },
    )


def logout_view(request):
    logout(request)
    return redirect('index')


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect('index')

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.get_user()
        login(request, user)
        _get_profile(user)
        return redirect('index')

    return render(request, 'taxi_app/auth_login.html', {'form': form})


@ensure_csrf_cookie
def register_view(request):
    if request.user.is_authenticated:
        return redirect('index')

    form = UserCreationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        login(request, user)
        _get_profile(user)
        request.session['show_role_confirmation'] = True
        return redirect('index')

    return render(request, 'taxi_app/auth_register.html', {'form': form})


def _telegram_payload_as_dict(payload):
    """DRF: JSON — звичайний mapping; форма POST — часто QueryDict з методом .dict()."""
    as_dict_fn = getattr(payload, "dict", None)
    if callable(as_dict_fn) and not isinstance(payload, dict):
        try:
            return dict(as_dict_fn())
        except (TypeError, AttributeError):
            pass
    return dict(payload.items())


def _complete_telegram_login(request, data):
    """
    Перевіряє підпис Telegram Login і створює/оновлює користувача.
    Повертає (success: bool, error_response_or_none для API, або None якщо API не використовуватиметься)
    При успіху: login(request), можливий прапорець сесії.
    """
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        return False, Response({"error": "Bot token not found"}, status=500)

    data = dict(data)
    check_hash = data.pop('hash', None)
    if not check_hash:
        return False, Response({"error": "Hash missing"}, status=400)

    auth_date_raw = data.get('auth_date')
    if auth_date_raw is None:
        return False, Response({"error": "auth_date missing"}, status=400)
    try:
        auth_date = int(auth_date_raw)
    except (TypeError, ValueError):
        return False, Response({"error": "Invalid auth_date"}, status=400)
    if abs(int(time.time()) - auth_date) > 86400:
        return False, Response({"error": "auth_date expired"}, status=400)

    check_data = {}
    for key, raw in sorted(data.items()):
        if raw is None or raw == "":
            continue
        check_data[key] = str(raw)

    data_check_string = "\n".join(f"{k}={v}" for k, v in check_data.items())
    secret_key = hashlib.sha256(bot_token.encode()).digest()

    generated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(generated_hash, str(check_hash)):
        return False, Response(
            {"status": "error", "message": "Invalid Telegram auth"},
            status=400,
        )

    tg_id = check_data.get("id")
    if tg_id is None:
        return False, Response({"error": "id missing"}, status=400)

    user, created = User.objects.get_or_create(
        username=f"tg_{tg_id}",
        defaults={"first_name": check_data.get("first_name", "")},
    )

    if not created:
        fn = check_data.get("first_name")
        if fn and user.first_name != fn:
            user.first_name = fn
            user.save()

    profile, p_created = UserProfile.objects.get_or_create(
        user=user,
        defaults={
            "telegram_id": int(tg_id),
            "role": "passenger",
        },
    )
    if not p_created:
        tid = int(tg_id)
        if profile.telegram_id != tid:
            profile.telegram_id = tid
            profile.save()

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    if created:
        request.session['show_role_confirmation'] = True

    return True, None


@require_GET
def telegram_login_callback(request):
    """Legacy-віджет у режимі «Redirect»: без iframe oauth — обходимо CSP frame-ancestors (порт/host)."""
    data = request.GET.dict()

    ok, err_response = _complete_telegram_login(request, data)
    if not ok:
        reason = ""
        if err_response is not None:
            reason = str(getattr(err_response, "data", {}).get("error", "") or "")
        suffix = f"&tg_reason={quote_plus(reason)}" if reason else ""
        return redirect(reverse('index') + f'?tg_err=1{suffix}')
    app_url = reverse("index")
    return HttpResponse(
        f"""
        <!doctype html>
        <html>
        <body>
        <script>
          try {{
            if (window.opener) {{
              window.opener.location.href = "{app_url}";
              window.close();
            }} else {{
              window.location.href = "{app_url}";
            }}
          }} catch (e) {{
            window.location.href = "{app_url}";
          }}
        </script>
        </body>
        </html>
        """.strip()
    )


@api_view(['POST'])
def telegram_auth(request):
    data = _telegram_payload_as_dict(request.data)
    ok, err_response = _complete_telegram_login(request, data)
    if not ok:
        return err_response
    return Response({"status": "success"})

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_order_api(request):
    try:
        data = request.data
        start_lat = data.get('start_lat')
        start_lon = data.get('start_lon')
        end_lat = data.get('end_lat')
        end_lon = data.get('end_lon')
        service_tier = data.get('service_tier', 'standard')
        driver_user_id = data.get("driver_user_id")
        payment_method = data.get("payment_method", "cash")
        promo_code_raw = (data.get("promo_code") or "").strip().upper()

        if service_tier not in TIER_RULES:
            return Response({"error": "Невідомий клас поїздки"}, status=400)
        if payment_method not in ("cash", "card"):
            return Response({"error": "Спосіб оплати: cash або card"}, status=400)

        if not all([start_lat, start_lon, end_lat, end_lon]):
            return Response({"error": "Missing coordinates"}, status=400)

        distance_km, _duration_mins = get_route_info(
            float(start_lat), float(start_lon), float(end_lat), float(end_lon)
        )
        if distance_km is None:
            return Response({"error": "Could not compute route"}, status=502)

        price_info = calculate_price_details(distance_km, tier=service_tier)
        if not price_info:
            return Response({"error": "Could not compute price"}, status=502)
        price = price_info["total"]
        promo_applied = None
        if promo_code_raw:
            promo = PromoCode.objects.filter(code=promo_code_raw, is_active=True).first()
            if promo is None:
                return Response({"error": "Промокод не знайдено або неактивний"}, status=400)
            if promo.expires_at and promo.expires_at < timezone.now():
                return Response({"error": "Термін дії промокоду минув"}, status=400)
            discount = max(0, min(100, int(promo.discount_percent)))
            price = round(price * (100 - discount) / 100, 2)
            promo_applied = {"code": promo.code, "discount_percent": discount}

        user = request.user
        passenger_name = (user.first_name or "").strip() or user.username

        passenger_profile = _get_profile(user)
        if passenger_profile.role != "passenger":
            return Response({"error": "Створювати замовлення може лише пасажир"}, status=403)

        driver = None
        if driver_user_id not in (None, "", 0):
            try:
                driver_user_id = int(driver_user_id)
            except (TypeError, ValueError):
                return Response({"error": "Невірний driver_user_id"}, status=400)
            driver_profile = UserProfile.objects.filter(
                user_id=driver_user_id, role="driver", is_online=True
            ).select_related("user").first()
            if driver_profile is None:
                return Response({"error": "Водій недоступний"}, status=400)
            driver = driver_profile.user
        elif start_lat and start_lon:
            # Якщо пасажир не вибрав водія руками, беремо найближчого онлайн по координатах.
            candidate_profiles = UserProfile.objects.select_related("user").filter(
                role="driver",
                is_online=True,
                lat__isnull=False,
                lon__isnull=False,
            )
            best_profile = None
            best_dist = None
            for candidate in candidate_profiles:
                dist = _haversine_km(
                    float(start_lat), float(start_lon), candidate.lat, candidate.lon
                )
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_profile = candidate
            if best_profile:
                driver = best_profile.user

        explicit_driver_selected = driver_user_id not in (None, "", 0)
        
        deposit_amount = 0.00
        if payment_method == "card":
            deposit_amount = min(float(price), 100.0)
            payment_status = "pending_deposit"
            status = "new"
        else:
            payment_status = "not_required"
            status = "searching"

        order = Order.objects.create(
            passenger=user,
            driver=driver,
            passenger_name=passenger_name,
            service_tier=service_tier,
            start_lat=start_lat,
            start_lon=start_lon,
            end_lat=end_lat,
            end_lon=end_lon,
            distance=distance_km,
            price=price,
            payment_method=payment_method,
            payment_status=payment_status,
            deposit_amount=deposit_amount,
            status=status,
        )

        checkout_url = None
        if payment_method == "card":
            checkout_res = _create_stripe_checkout(order, request, deposit_amount, "deposit")
            if checkout_res.get("error"):
                order.payment_status = "failed"
                order.save(update_fields=["payment_status"])
                return Response({"error": checkout_res["error"]}, status=400)
            order.paddle_transaction_id = checkout_res.get("transaction_id", "")
            order.save(update_fields=["paddle_transaction_id"])
            checkout_url = checkout_res.get("checkout_url")

        return Response({
            "status": "success",
            "order_id": order.id,
            "price": price,
            "currency": DEFAULT_CURRENCY,
            "payment_method": payment_method,
            "payment_status": order.payment_status,
            "payment_url": checkout_url,
            "deposit_amount": float(deposit_amount),
            "assigned_driver": (
                {
                    "user_id": driver.id,
                    "name": driver.first_name or driver.username,
                    "is_bot": bool(
                        UserProfile.objects.filter(user=driver, is_bot_driver=True).exists()
                    ),
                }
                if driver
                else None
            ),
            "driver_request_sent": bool(explicit_driver_selected and driver is not None),
            "promo_applied": promo_applied,
        })

    except Exception as e:
        print(f"Error creating order: {e}")
        return Response({"status": "error", "message": str(e)}, status=500)


@api_view(['GET'])
@permission_classes([AllowAny])
def estimate_price_api(request):
    """
    Public endpoint для попереднього розрахунку ціни.
    Працює без логіна, щоб фронт не падав з "Authentication credentials were not provided".
    """
    try:
        start_lat = request.query_params.get('start_lat')
        start_lon = request.query_params.get('start_lon')
        end_lat = request.query_params.get('end_lat')
        end_lon = request.query_params.get('end_lon')
        service_tier = request.query_params.get('service_tier', 'standard')

        if service_tier not in TIER_RULES:
            return Response({"error": "Невідомий клас поїздки"}, status=400)
        if not all([start_lat, start_lon, end_lat, end_lon]):
            return Response({"error": "Missing coordinates"}, status=400)

        distance_km, duration_mins = get_route_info(
            float(start_lat), float(start_lon), float(end_lat), float(end_lon)
        )
        if distance_km is None:
            return Response({"error": "Could not compute route"}, status=502)

        price_info = calculate_price_details(distance_km, tier=service_tier)
        if not price_info:
            return Response({"error": "Could not compute price"}, status=502)

        return Response({
            "status": "ok",
            "price": price_info["total"],
            "currency": price_info["currency"],
            "distance_km": price_info["distance_km"],
            "duration_mins": round(float(duration_mins), 1) if duration_mins is not None else None,
            "tier": service_tier,
        })
    except (TypeError, ValueError):
        return Response({"error": "Invalid coordinates"}, status=400)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_me(request):
    p = _get_profile(request.user)
    return Response(
        {
            "id": request.user.pk,
            "first_name": request.user.first_name or "",
            "username": request.user.username,
            "role": p.role,
            "is_online": p.is_online,
            "tariff_plan": p.tariff_plan,
            "phone": p.phone,
            "location_city": p.location_city,
            "lat": p.lat,
            "lon": p.lon,
            "car_make": p.car_make,
            "car_model": p.car_model,
            "car_plate": p.car_plate,
            "driver_bio": p.driver_bio,
        }
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_set_role(request):
    role = request.data.get('role')
    if role not in ('passenger', 'driver', 'support'):
        return Response({"error": "Невідома роль"}, status=400)
    if role == "support" and not (request.user.is_staff or request.user.is_superuser):
        return Response({"error": "Роль support може призначати тільки адміністратор"}, status=403)
    p = _get_profile(request.user)
    p.role = role
    p.save(update_fields=['role'])
    request.session.pop('show_role_confirmation', None)
    return Response({"status": "ok", "role": p.role})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_driver_shift(request):
    action = request.data.get('action')
    p = _get_profile(request.user)
    if p.role != 'driver':
        return Response({"error": "Режим доступний лише для водіїв"}, status=403)
    if action == 'start':
        p.is_online = True
    elif action == 'end':
        p.is_online = False
    else:
        return Response({"error": "Невірна дія (start або end)"}, status=400)
    p.save(update_fields=['is_online'])
    return Response({"status": "ok", "is_online": p.is_online})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_driver_tariff(request):
    plan = request.data.get('tariff_plan')
    if plan not in TIER_RULES:
        return Response({"error": "Невідомий тариф"}, status=400)
    p = _get_profile(request.user)
    if p.role != 'driver':
        return Response({"error": "Тариф обирають лише у режимі водія"}, status=403)
    p.tariff_plan = plan
    p.save(update_fields=['tariff_plan'])
    return Response({"status": "ok", "tariff_plan": p.tariff_plan})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_drivers_online(request):
    """Список онлайн-водіїв для пасажира (MVP без геофільтра)."""
    passenger_profile = _get_profile(request.user)
    if passenger_profile.role != "passenger":
        return Response({"error": "Доступно лише для пасажира"}, status=403)

    drivers = (
        UserProfile.objects.select_related("user")
        .filter(role="driver", is_online=True)
        .order_by("user__first_name", "user__username")[:100]
    )
    items = []
    for d in drivers:
        items.append(
            {
                "user_id": d.user_id,
                "name": (d.user.first_name or d.user.username),
                "tariff_plan": d.tariff_plan,
                "phone": d.phone,
                "car": " ".join(x for x in [d.car_make, d.car_model] if x).strip(),
                "plate": d.car_plate,
                "bio": d.driver_bio,
            }
        )
    return Response({"items": items})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_passenger_profile_update(request):
    p = _get_profile(request.user)
    if p.role != "passenger":
        return Response({"error": "Доступно лише для пасажира"}, status=403)

    phone = (request.data.get("phone") or "").strip()
    location_city = (request.data.get("location_city") or "").strip()
    lat = None
    lon = None
    if location_city:
        lat, lon = geocode_city(location_city)
        if lat is None or lon is None:
            return Response({"error": "Не вдалося знайти це місто"}, status=400)

    p.phone = phone[:32]
    p.location_city = location_city[:120]
    p.lat = lat
    p.lon = lon
    p.save(update_fields=["phone", "location_city", "lat", "lon"])
    return Response({"status": "ok", "phone": p.phone, "location_city": p.location_city, "lat": p.lat, "lon": p.lon})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_drivers_map(request):
    """Повертає онлайн-водіїв для мапи, включно з ботами."""
    _ensure_bot_drivers()
    bots = UserProfile.objects.select_related("user").filter(is_bot_driver=True, is_online=True)
    for bot in bots:
        _move_bot_driver(bot)

    drivers = (
        UserProfile.objects.select_related("user")
        .filter(role="driver", is_online=True, lat__isnull=False, lon__isnull=False)
        .order_by("user__first_name", "user__username")[:200]
    )
    items = []
    for d in drivers:
        items.append(
            {
                "user_id": d.user_id,
                "name": d.user.first_name or d.user.username,
                "lat": d.lat,
                "lon": d.lon,
                "tariff_plan": d.tariff_plan,
                "is_bot": d.is_bot_driver,
                "car": " ".join(x for x in [d.car_make, d.car_model] if x).strip(),
                "plate": d.car_plate,
            }
        )
    return Response({"items": items})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_driver_profile_update(request):
    """Налаштування водія (контакти/авто/опис)."""
    p = _get_profile(request.user)
    if p.role != "driver":
        return Response({"error": "Доступно лише для водія"}, status=403)

    phone = (request.data.get("phone") or "").strip()
    car_make = (request.data.get("car_make") or "").strip()
    car_model = (request.data.get("car_model") or "").strip()
    car_plate = (request.data.get("car_plate") or "").strip().upper()
    driver_bio = (request.data.get("driver_bio") or "").strip()

    p.phone = phone[:32]
    p.car_make = car_make[:64]
    p.car_model = car_model[:64]
    p.car_plate = car_plate[:16]
    p.driver_bio = driver_bio[:160]
    p.save(update_fields=["phone", "car_make", "car_model", "car_plate", "driver_bio"])

    return Response({"status": "ok"})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_orders_history(request):
    p = _get_profile(request.user)
    if p.role == "driver":
        qs = Order.objects.filter(driver=request.user)
    else:
        qs = Order.objects.filter(passenger=request.user)
    qs = qs.select_related("driver", "passenger").order_by("-created_at")[:50]
    
    status_map = {
        'new': 'Новий',
        'searching': 'Пошук водія',
        'accepted': 'Прийнято водієм',
        'waiting': 'Очікування пасажира',
        'in_progress': 'У дорозі',
        'completed': 'Завершено',
        'cancelled': 'Скасовано',
    }
    
    pay_status_map = {
        'not_required': 'Не потрібно',
        'pending': 'Очікує оплати',
        'pending_deposit': 'Очікує передплати',
        'deposit_paid': 'Передплачено (100 грн)',
        'pending_remainder': 'Очікує доплати',
        'paid': 'Оплачено повністю',
        'failed': 'Помилка оплати',
    }

    tier_map = {
        'economy': 'Економ',
        'standard': 'Стандарт',
        'comfort': 'Комфорт',
    }

    items = []
    for o in qs:
        driver_name = ""
        if o.driver:
            driver_name = o.driver.first_name or o.driver.username
            
        items.append(
            {
                "id": o.id,
                "status": o.status,
                "status_label": status_map.get(o.status, o.status),
                "service_tier": o.service_tier,
                "service_tier_label": tier_map.get(o.service_tier, o.service_tier),
                "price": float(o.price) if o.price is not None else 0.0,
                "payment_method": o.payment_method,
                "payment_method_label": 'Картка' if o.payment_method == 'card' else 'Готівка',
                "payment_status": o.payment_status,
                "payment_status_label": pay_status_map.get(o.payment_status, o.payment_status),
                "created_at": o.created_at.isoformat(),
                "created_at_label": o.created_at.strftime("%d.%m.%Y %H:%M"),
                "start_lat": o.start_lat,
                "start_lon": o.start_lon,
                "end_lat": o.end_lat,
                "end_lon": o.end_lon,
                "distance": o.distance,
                "waiting_seconds": o.waiting_seconds,
                "waiting_price": float(o.waiting_price),
                "driver_name": driver_name,
                "passenger_name": o.passenger_name,
            }
        )
    return Response({"items": items})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def api_support_tickets(request):
    if request.method == "GET":
        if _is_support_or_admin(request.user):
            qs = SupportTicket.objects.select_related("author").order_by("-created_at")[:100]
        else:
            qs = SupportTicket.objects.select_related("author").filter(author=request.user).order_by("-created_at")[:100]
        items = []
        for t in qs:
            items.append(
                {
                    "id": t.id,
                    "subject": t.subject,
                    "status": t.status,
                    "author": t.author.username,
                    "created_at": t.created_at.isoformat(),
                }
            )
        return Response({"items": items})

    subject = (request.data.get("subject") or "").strip()
    body = (request.data.get("message") or "").strip()
    if not subject or not body:
        return Response({"error": "subject і message обов'язкові"}, status=400)
    ticket = SupportTicket.objects.create(author=request.user, subject=subject[:160], status="open")
    SupportMessage.objects.create(ticket=ticket, author=request.user, body=body)
    return Response({"status": "ok", "ticket_id": ticket.id})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def api_support_ticket_messages(request, ticket_id):
    ticket = SupportTicket.objects.select_related("author").filter(pk=ticket_id).first()
    if ticket is None:
        return Response({"error": "Тікет не знайдено"}, status=404)
    can_moderate = _is_support_or_admin(request.user)
    if not can_moderate and ticket.author_id != request.user.id:
        return Response({"error": "Немає доступу до тікета"}, status=403)

    if request.method == "GET":
        msgs = ticket.messages.select_related("author").order_by("created_at")
        return Response(
            {
                "ticket": {
                    "id": ticket.id,
                    "subject": ticket.subject,
                    "status": ticket.status,
                    "author": ticket.author.username,
                },
                "items": [
                    {
                        "id": m.id,
                        "author": m.author.username,
                        "is_support_reply": _is_support_or_admin(m.author),
                        "body": m.body,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in msgs
                ],
            }
        )

    body = (request.data.get("message") or "").strip()
    if not body:
        return Response({"error": "message обов'язкове"}, status=400)
    if ticket.status != "open" and not can_moderate:
        return Response({"error": "Тікет закрито"}, status=400)
    SupportMessage.objects.create(ticket=ticket, author=request.user, body=body)
    new_status = (request.data.get("status") or "").strip().lower()
    if can_moderate and new_status in ("open", "closed") and new_status != ticket.status:
        ticket.status = new_status
        ticket.save(update_fields=["status"])
    return Response({"status": "ok"})


@csrf_exempt
def stripe_webhook(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        return JsonResponse({"error": "Webhook secret is not configured"}, status=500)
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=secret)
    except Exception:
        return JsonResponse({"error": "Invalid signature"}, status=400)

    if event.get("type") == "checkout.session.completed":
        session = (event.get("data") or {}).get("object") or {}
        order_id = (session.get("metadata") or {}).get("order_id") or session.get("client_reference_id")
        payment_type = (session.get("metadata") or {}).get("payment_type", "deposit")
        if order_id:
            order = Order.objects.filter(pk=order_id).first()
            if order:
                if payment_type == "deposit":
                    order.payment_status = "deposit_paid"
                    order.status = "searching"
                    order.paddle_transaction_id = session.get("id", "")[:64]
                    order.save(update_fields=["payment_status", "status", "paddle_transaction_id"])
                else:
                    order.payment_status = "paid"
                    order.paddle_transaction_id = session.get("id", "")[:64]
                    order.save(update_fields=["payment_status", "paddle_transaction_id"])
    return JsonResponse({"received": True})


def _simulate_bot_order(order):
    """
    Симулює рух та життєвий цикл замовлення для водіїв-ботів у реальному часі.
    Слідує новій схемі статусів (searching -> accepted -> waiting -> in_progress -> completed).
    """
    try:
        driver_profile = order.driver.userprofile if order.driver else None
    except Exception:
        driver_profile = None

    elapsed = (timezone.now() - order.created_at).total_seconds()

    if order.status == 'new':
        if order.payment_method == 'card' and elapsed >= 2:
            order.payment_status = 'deposit_paid'
            order.status = 'searching'
            order.save(update_fields=['payment_status', 'status'])

    elif order.status == 'searching':
        if elapsed >= 3:
            bot_profile = UserProfile.objects.select_related("user").filter(
                role="driver",
                is_bot_driver=True,
                is_online=True
            ).first()
            if bot_profile:
                order.driver = bot_profile.user
                order.status = 'accepted'
                order.save(update_fields=['driver', 'status'])
                
                bot_profile.lat = order.start_lat + random.uniform(-0.005, 0.005)
                bot_profile.lon = order.start_lon + random.uniform(-0.005, 0.005)
                bot_profile.save(update_fields=['lat', 'lon'])

    elif order.status == 'accepted':
        if elapsed >= 7:
            order.is_waiting = True
            order.waiting_started_at = timezone.now()
            order.status = 'waiting'
            order.save(update_fields=['is_waiting', 'waiting_started_at', 'status'])
            
            if driver_profile:
                driver_profile.lat = order.start_lat
                driver_profile.lon = order.start_lon
                driver_profile.save(update_fields=['lat', 'lon'])

    elif order.status == 'waiting':
        if elapsed >= 13:
            order.waiting_seconds += 6
            order.waiting_price = (order.waiting_seconds // 10) * 2
            order.is_waiting = False
            order.waiting_started_at = None
            order.status = 'in_progress'
            order.save(update_fields=['waiting_seconds', 'waiting_price', 'is_waiting', 'waiting_started_at', 'status'])

    elif order.status == 'in_progress':
        trip_duration = 10.0
        progress = min(1.0, (elapsed - 13.0) / trip_duration)

        if progress >= 1.0:
            order.status = 'completed'
            orig_price = float(order.price) if order.price is not None else 0.0
            waiting_pr = float(order.waiting_price)
            final_price = orig_price + waiting_pr
            order.price = final_price
            order.remainder_amount = max(0.0, final_price - float(order.deposit_amount))
            order.payment_status = 'paid'
            order.save(update_fields=['status', 'payment_status', 'price', 'remainder_amount'])
            
            if driver_profile:
                driver_profile.lat = order.end_lat
                driver_profile.lon = order.end_lon
                driver_profile.save(update_fields=['lat', 'lon'])
        else:
            if driver_profile:
                driver_profile.lat = order.start_lat + (order.end_lat - order.start_lat) * progress
                driver_profile.lon = order.start_lon + (order.end_lon - order.start_lon) * progress
                driver_profile.save(update_fields=['lat', 'lon'])


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_active_order(request):
    """
    Повертає поточне активне замовлення пасажира або водія.
    """
    user = request.user
    profile = _get_profile(user)

    order = None
    is_offer = False

    if profile.role == 'driver':
        order = Order.objects.filter(
            driver=user,
            status__in=['accepted', 'waiting', 'in_progress']
        ).order_by('-created_at').first()

        if not order:
            offer_order = Order.objects.filter(
                status='searching',
                driver__isnull=True
            ).exclude(declined_by=user).order_by('-created_at').first()

            if not offer_order:
                offer_order = Order.objects.filter(
                    status='searching',
                    driver=user
                ).exclude(declined_by=user).order_by('-created_at').first()

            if offer_order:
                order = offer_order
                is_offer = True
    else:
        order = Order.objects.filter(
            passenger=user,
            status__in=['new', 'searching', 'accepted', 'waiting', 'in_progress']
        ).order_by('-created_at').first()

        if not order:
            order = Order.objects.filter(
                passenger=user,
                status='completed',
                payment_status='pending_remainder'
            ).order_by('-created_at').first()

    if order:
        if order.driver and getattr(order.driver, 'userprofile', None) and order.driver.userprofile.is_bot_driver:
            _simulate_bot_order(order)

        driver_info = None
        if order.driver:
            dp = _get_profile(order.driver)
            driver_info = {
                "user_id": order.driver.id,
                "name": order.driver.first_name or order.driver.username,
                "phone": dp.phone,
                "car": " ".join(x for x in [dp.car_make, dp.car_model] if x).strip(),
                "plate": dp.car_plate,
                "bio": dp.driver_bio,
                "lat": dp.lat,
                "lon": dp.lon,
                "is_bot": dp.is_bot_driver,
            }

        waiting_sec = order.waiting_seconds
        if order.is_waiting and order.waiting_started_at:
            elapsed = int((timezone.now() - order.waiting_started_at).total_seconds())
            waiting_sec += elapsed
        
        waiting_pr = (waiting_sec // 10) * 2
        orig_price = float(order.price) if order.price is not None else 0.0
        total_price = orig_price + float(waiting_pr)

        remainder_checkout_url = None
        if order.payment_status == 'pending_remainder' and order.remainder_amount > 0:
            checkout_res = _create_stripe_checkout(order, request, order.remainder_amount, "remainder")
            if not checkout_res.get("error"):
                remainder_checkout_url = checkout_res.get("checkout_url")

        return Response({
            "status": "success",
            "has_active": True,
            "is_offer": is_offer,
            "order": {
                "id": order.id,
                "status": order.status,
                "passenger_id": order.passenger_id if order.passenger else None,
                "passenger_name": order.passenger_name,
                "service_tier": order.service_tier,
                "start_lat": order.start_lat,
                "start_lon": order.start_lon,
                "end_lat": order.end_lat,
                "end_lon": order.end_lon,
                "distance": order.distance,
                "payment_method": order.payment_method,
                "payment_status": order.payment_status,
                "driver": driver_info,
                "created_at": order.created_at.isoformat(),
                "waiting_seconds": waiting_sec,
                "waiting_price": float(waiting_pr),
                "is_waiting": order.is_waiting,
                "original_price": orig_price,
                "price": total_price,
                "deposit_amount": float(order.deposit_amount),
                "remainder_amount": float(order.remainder_amount),
                "remainder_checkout_url": remainder_checkout_url,
            }
        })

    return Response({
        "status": "success",
        "has_active": False
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_order_action(request, order_id):
    """
    Виконує дію (прийняти, відхилити, почати очікування, завершити тощо) над конкретним замовленням.
    """
    action = request.data.get('action')
    allowed_actions = ('accept', 'decline', 'start_waiting', 'stop_waiting', 'start_ride', 'complete', 'cancel')
    if action not in allowed_actions:
        return Response({"error": "Невідома дія"}, status=400)

    order = Order.objects.filter(pk=order_id).first()
    if not order:
        return Response({"error": "Замовлення не знайдено"}, status=404)

    user = request.user
    profile = _get_profile(user)

    if action == 'accept':
        if profile.role != 'driver':
            return Response({"error": "Тільки водій може прийняти замовлення"}, status=403)
        if order.status != 'searching':
            return Response({"error": "Замовлення не очікує прийняття"}, status=400)
        if order.driver and order.driver != user:
            return Response({"error": "Це замовлення вже прийнято іншим водієм"}, status=400)

        order.driver = user
        order.status = 'accepted'
        order.save(update_fields=['driver', 'status'])

    elif action == 'decline':
        if profile.role != 'driver':
            return Response({"error": "Тільки водій може відхилити замовлення"}, status=403)
        if order.status != 'searching':
            return Response({"error": "Замовлення не очікує прийняття"}, status=400)

        order.declined_by.add(user)
        
        if order.driver == user:
            order.driver = None
            order.save(update_fields=['driver'])

    elif action == 'start_waiting':
        if profile.role != 'driver':
            return Response({"error": "Тільки водій може керувати очікуванням"}, status=403)
        if order.driver != user:
            return Response({"error": "Ви не призначені водієм для цього замовлення"}, status=403)
        if order.is_waiting:
            return Response({"error": "Очікування вже запущено"}, status=400)

        order.is_waiting = True
        order.waiting_started_at = timezone.now()
        order.status = 'waiting'
        order.save(update_fields=['is_waiting', 'waiting_started_at', 'status'])

    elif action == 'stop_waiting':
        if profile.role != 'driver':
            return Response({"error": "Тільки водій може керувати очікуванням"}, status=403)
        if order.driver != user:
            return Response({"error": "Ви не призначені водієм для цього замовлення"}, status=403)
        if not order.is_waiting:
            return Response({"error": "Очікування не запущено"}, status=400)

        elapsed = 0
        if order.waiting_started_at:
            elapsed = int((timezone.now() - order.waiting_started_at).total_seconds())

        order.waiting_seconds += elapsed
        order.waiting_price = (order.waiting_seconds // 10) * 2
        order.is_waiting = False
        order.waiting_started_at = None
        if order.status == 'waiting':
            order.status = 'accepted'
        
        order.save(update_fields=['waiting_seconds', 'waiting_price', 'is_waiting', 'waiting_started_at', 'status'])

    elif action == 'start_ride':
        if profile.role != 'driver':
            return Response({"error": "Тільки водій може розпочати поїздку"}, status=403)
        if order.driver != user:
            return Response({"error": "Ви не призначені водієм для цього замовлення"}, status=403)
        if order.status not in ('accepted', 'waiting'):
            return Response({"error": "Неможливо розпочати поїздку з цього статусу"}, status=400)

        if order.is_waiting:
            elapsed = 0
            if order.waiting_started_at:
                elapsed = int((timezone.now() - order.waiting_started_at).total_seconds())
            order.waiting_seconds += elapsed
            order.waiting_price = (order.waiting_seconds // 10) * 2
            order.is_waiting = False
            order.waiting_started_at = None

        order.status = 'in_progress'
        order.save(update_fields=['status', 'waiting_seconds', 'waiting_price', 'is_waiting', 'waiting_started_at'])

    elif action == 'complete':
        if profile.role != 'driver':
            return Response({"error": "Тільки водій може завершити поїздку"}, status=403)
        if order.driver != user:
            return Response({"error": "Ви не призначені водієм для цього замовлення"}, status=403)
        if order.status not in ('accepted', 'waiting', 'in_progress'):
            return Response({"error": "Поїздка не в процесі виконання"}, status=400)

        if order.is_waiting:
            elapsed = 0
            if order.waiting_started_at:
                elapsed = int((timezone.now() - order.waiting_started_at).total_seconds())
            order.waiting_seconds += elapsed
            order.is_waiting = False
            order.waiting_started_at = None

        orig_price = float(order.price) if order.price is not None else 0.0
        waiting_pr = float((order.waiting_seconds // 10) * 2)
        final_price = orig_price + waiting_pr

        order.price = final_price
        order.waiting_price = waiting_pr
        order.remainder_amount = max(0.0, final_price - float(order.deposit_amount))
        
        order.status = 'completed'
        if order.payment_method == 'card':
            if order.remainder_amount > 0:
                order.payment_status = 'pending_remainder'
            else:
                order.payment_status = 'paid'
        else:
            order.payment_status = 'paid'

        order.save(update_fields=['status', 'payment_status', 'price', 'waiting_price', 'waiting_seconds', 'is_waiting', 'waiting_started_at', 'remainder_amount'])

    elif action == 'cancel':
        if order.is_waiting:
            order.is_waiting = False
            order.waiting_started_at = None
        
        if profile.role == 'driver':
            if order.driver != user:
                return Response({"error": "Ви не можете скасувати чуже замовлення"}, status=403)
            if order.status not in ('accepted', 'waiting', 'in_progress'):
                return Response({"error": "Неможливо скасувати замовлення в поточному статусі"}, status=400)
        else:
            if order.passenger != user:
                return Response({"error": "Ви не можете скасувати чуже замовлення"}, status=403)
            if order.status not in ('new', 'searching'):
                return Response({"error": "Неможливо скасувати замовлення, оскільки водій уже в дорозі"}, status=400)

        order.status = 'cancelled'
        order.save(update_fields=['status', 'is_waiting', 'waiting_started_at'])

    return Response({"status": "success", "order_status": order.status})