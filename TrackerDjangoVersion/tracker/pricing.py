from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import PricingConfig


@dataclass(frozen=True)
class PricingState:
    total_users: int
    promo_active: bool
    promo_remaining: int
    promo_limit: int
    monthly_price: Decimal
    annual_price: Decimal
    regular_monthly_price: Decimal
    regular_annual_price: Decimal
    promo_monthly_price: Decimal
    promo_annual_price: Decimal
    promo_label: str


def _count_users() -> int:
    user_model = get_user_model()
    return (
        user_model.objects.filter(is_active=True, profile__payment_confirmed=True)
        .exclude(is_superuser=True)
        .exclude(profile__is_guest=True)
        .count()
    )


def get_pricing_state() -> PricingState:
    config = PricingConfig.get_solo()
    total_users = _count_users()
    promo_remaining = max(config.promo_limit - total_users, 0)
    promo_active = bool(config.promo_active and promo_remaining > 0)
    monthly_price = config.promo_monthly_price if promo_active else config.regular_monthly_price
    annual_price = config.promo_annual_price if promo_active else config.regular_annual_price
    return PricingState(
        total_users=total_users,
        promo_active=promo_active,
        promo_remaining=promo_remaining,
        promo_limit=config.promo_limit,
        monthly_price=monthly_price,
        annual_price=annual_price,
        regular_monthly_price=config.regular_monthly_price,
        regular_annual_price=config.regular_annual_price,
        promo_monthly_price=config.promo_monthly_price,
        promo_annual_price=config.promo_annual_price,
        promo_label=config.promo_label,
    )
