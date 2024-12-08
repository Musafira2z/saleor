from django.core.management.base import BaseCommand
from datetime import timedelta
from django.utils.timezone import now

from saleor.order.models import Order
from saleor.order.tasks import check_and_cancel_orders


class Command(BaseCommand):
    help = "Test the auto-cancel functionality for orders."

    def handle(self, *args, **options):
        # Call the auto-cancel function (adjust based on Saleor's implementation)
        check_and_cancel_orders()
