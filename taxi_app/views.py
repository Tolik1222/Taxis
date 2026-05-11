import os
import time
import hmac
import hashlib
import math
import random
import stripe

from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Order, UserProfile
from .utils import TIER_RULES, get_route_info, calculate_price


def _get_profile(user):
    profile = UserProfile.objects.filter(user=user).first()
    if profile is None:
        profile = UserProfile.objects.create(user=user, role='passenger')
    return profile


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


def _create_stripe_checkout(order, request):
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
    amount_minor = int(float(order.price) * 100)
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
            },
            line_items=[
                {
                    "price_data": {
                        "currency": currency,
                        "product_data": {
                            "name": f"Taxi ride #{order.id}",
                            "description": f"Поїздка класу {order.service_tier}",
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

    login(request, user)
    if created:
        request.session['show_role_confirmation'] = True

    return True, None


@require_GET
def telegram_login_callback(request):
    """Legacy-віджет у режимі «Redirect»: без iframe oauth — обходимо CSP frame-ancestors (порт/host)."""
    data = request.GET.dict()

    ok, err_response = _complete_telegram_login(request, data)
    if not ok:
        return redirect(reverse('index') + '?tg_err=1')
    return redirect('index')


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

        price = calculate_price(distance_km, tier=service_tier)

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
            payment_status="pending" if payment_method == "card" else "not_required",
            status="new",
        )

        checkout_url = None
        if payment_method == "card":
            checkout_res = _create_stripe_checkout(order, request)
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
            "payment_method": payment_method,
            "payment_status": order.payment_status,
            "payment_url": checkout_url,
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
        })

    except Exception as e:
        print(f"Error creating order: {e}")
        return Response({"status": "error", "message": str(e)}, status=500)


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
    if role not in ('passenger', 'driver'):
        return Response({"error": "Невідома роль"}, status=400)
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