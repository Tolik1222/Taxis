import os
import time
import hmac
import hashlib

from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET
from django.contrib.auth import login, logout
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


@ensure_csrf_cookie
def index_page(request):
    profile = None
    if request.user.is_authenticated:
        profile = _get_profile(request.user)
    needs_role_gate = request.session.get('show_role_confirmation', False)
    telegram_bot_username = os.getenv('TELEGRAM_BOT_USERNAME', 'TolikTaxi1_Bot')
    telegram_redirect_uri = request.build_absolute_uri(reverse('telegram_callback'))
    return render(
        request,
        'taxi_app/index.html',
        {
            'profile': profile,
            'needs_role_gate': needs_role_gate,
            'telegram_bot_username': telegram_bot_username,
            'telegram_redirect_uri': telegram_redirect_uri,
            'telegram_login_hint': (
                not request.user.is_authenticated
                and request.GET.get('tg_err') == '1'
            ),
        },
    )


def logout_view(request):
    logout(request)
    return redirect('index')


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

        if service_tier not in TIER_RULES:
            return Response({"error": "Невідомий клас поїздки"}, status=400)

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

        order = Order.objects.create(
            passenger=user,
            passenger_name=passenger_name,
            service_tier=service_tier,
            start_lat=start_lat,
            start_lon=start_lon,
            end_lat=end_lat,
            end_lon=end_lon,
            distance=distance_km,
            price=price,
            status="new",
        )

        return Response({
            "status": "success",
            "order_id": order.id,
            "price": price,
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