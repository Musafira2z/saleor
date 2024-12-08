from datetime import timedelta
from typing import List

from django.db import transaction
from django.utils import timezone

from . import OrderStatus
from .fetch import fetch_order_lines
from ..celeryconf import app
from ..plugins.manager import get_plugins_manager
from .models import Order
from .utils import invalidate_order_prices
from ..warehouse.management import deallocate_stock


@app.task
def recalculate_orders_task(order_ids: List[int]):
    orders = Order.objects.filter(id__in=order_ids)

    for order in orders:
        invalidate_order_prices(order)

    Order.objects.bulk_update(orders, ["should_refresh_prices"])


@app.task
def send_order_updated(order_ids):
    manager = get_plugins_manager()
    for order in Order.objects.filter(id__in=order_ids):
        manager.order_updated(order)


@app.task
def check_and_cancel_orders():
    manager = get_plugins_manager()
    threshold_date = timezone.now() - timedelta(days=2)
    orders = Order.objects.filter(
        status__in=[OrderStatus.UNFULFILLED],
        created_at__lt=threshold_date,
    )
    for order in orders:
        try:
            with transaction.atomic():
                # Prepare OrderLineInfo objects
                order_lines_info = fetch_order_lines(order)

                # Deallocate stock
                deallocate_stock(order_lines_data=order_lines_info, manager=manager)

                # Update order status to canceled
                order.status = OrderStatus.CANCELED
                order.save(update_fields=["status"])
                print("done")

        except Exception as e:
            # Log errors for debugging
            print(f"Failed to cancel order {order.id}: {str(e)}")
