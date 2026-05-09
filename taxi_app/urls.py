from django.urls import path
from .views import (
    api_driver_shift,
    api_driver_tariff,
    api_me,
    api_set_role,
    create_order_api,
    index_page,
    logout_view,
    telegram_auth,
    telegram_login_callback,
)

urlpatterns = [
    path('', index_page, name='index'),
    path(
        'telegram/callback/',
        telegram_login_callback,
        name='telegram_callback',
    ),
    path('logout/', logout_view, name='logout'),
    path('api/create/', create_order_api, name='create_order'),
    path('api/auth/telegram/', telegram_auth, name='tg_auth'),
    path('api/me/', api_me, name='api_me'),
    path('api/session/role/', api_set_role, name='api_set_role'),
    path('api/driver/shift/', api_driver_shift, name='api_driver_shift'),
    path('api/driver/tariff/', api_driver_tariff, name='api_driver_tariff'),
]