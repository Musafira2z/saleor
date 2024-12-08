import uuid
from datetime import date, datetime
from tempfile import NamedTemporaryFile
from typing import IO, TYPE_CHECKING, Any, Dict, List, Set, Union

import petl as etl
from django.utils import timezone

from ...giftcard.models import GiftCard
from ...order import OrderStatus
from ...order.models import Order
from ...product.models import Product
from .. import FileTypes
from ..notifications import send_export_download_link_notification
from .product_headers import get_product_export_fields_and_headers_info
from .products_data import get_products_data

if TYPE_CHECKING:
    # flake8: noqa
    from django.db.models import QuerySet

    from ..models import ExportFile

BATCH_SIZE = 10000


def export_products(
        export_file: "ExportFile",
        scope: Dict[str, Union[str, dict]],
        export_info: Dict[str, list],
        file_type: str,
        delimiter: str = ",",
):
    from ...graphql.product.filters import ProductFilter

    file_name = get_filename("product", file_type)
    queryset = get_queryset(Product, ProductFilter, scope)

    (
        export_fields,
        file_headers,
        data_headers,
    ) = get_product_export_fields_and_headers_info(export_info)

    temporary_file = create_file_with_headers(file_headers, delimiter, file_type)

    export_products_in_batches(
        queryset,
        export_info,
        set(export_fields),
        data_headers,
        delimiter,
        temporary_file,
        file_type,
    )

    save_csv_file_in_export_file(export_file, temporary_file, file_name)
    temporary_file.close()

    send_export_download_link_notification(export_file, "products")


def export_gift_cards(
        export_file: "ExportFile",
        scope: Dict[str, Union[str, dict]],
        file_type: str,
        delimiter: str = ",",
):
    file_name = get_filename("gift_card", file_type)
    print("fileName: ", file_name)
    print(", filetype: ", file_type)
    print("\n")

    queryset = Order.objects.filter(
        created_at__date=date.today()
    )

    export_fields = ["number", "customer_name", "address", "total"]
    temporary_file = create_file_with_headers(export_fields, delimiter, file_type)

    export_gift_cards_in_batches(
        queryset,
        export_fields,
        delimiter,
        temporary_file,
        file_type,
    )

    save_csv_file_in_export_file(export_file, temporary_file, file_name)
    temporary_file.close()

    send_export_download_link_notification(export_file, "gift cards")


def get_filename(model_name: str, file_type: str) -> str:
    hash = uuid.uuid4()
    return "{}_data_{}_{}.{}".format(
        model_name, timezone.now().strftime("%d_%m_%Y_%H_%M_%S"), hash, file_type
    )


def get_queryset(model, filter, scope: Dict[str, Union[str, dict]]) -> "QuerySet":
    queryset = model.objects.all()
    if "ids" in scope:
        queryset = model.objects.filter(pk__in=scope["ids"])
    elif "filter" in scope:
        queryset = filter(data=parse_input(scope["filter"]), queryset=queryset).qs

    queryset = queryset.order_by("pk")

    return queryset


def parse_input(data: Any) -> Dict[str, Union[str, dict]]:
    """Parse input to correct data types, since scope coming from celery will be parsed to strings."""
    if "attributes" in data:
        serialized_attributes = []

        for attr in data.get("attributes") or []:
            if "date_time" in attr:
                if gte := attr["date_time"].get("gte"):
                    attr["date_time"]["gte"] = datetime.fromisoformat(gte)
                if lte := attr["date_time"].get("lte"):
                    attr["date_time"]["lte"] = datetime.fromisoformat(lte)

            if "date" in attr:
                if gte := attr["date"].get("gte"):
                    attr["date"]["gte"] = date.fromisoformat(gte)
                if lte := attr["date"].get("lte"):
                    attr["date"]["lte"] = date.fromisoformat(lte)

            serialized_attributes.append(attr)

        if serialized_attributes:
            data["attributes"] = serialized_attributes

    return data


def create_file_with_headers(file_headers: List[str], delimiter: str, file_type: str):
    table = etl.wrap([file_headers])

    if file_type == FileTypes.CSV:
        temp_file = NamedTemporaryFile("ab+", suffix=".csv")
        etl.tocsv(table, temp_file.name, delimiter=delimiter)
    else:
        temp_file = NamedTemporaryFile("ab+", suffix=".xlsx")
        etl.io.xlsx.toxlsx(table, temp_file.name)

    return temp_file


def export_products_in_batches(
        queryset: "QuerySet",
        export_info: Dict[str, list],
        export_fields: Set[str],
        headers: List[str],
        delimiter: str,
        temporary_file: Any,
        file_type: str,
):
    warehouses = export_info.get("warehouses")
    attributes = export_info.get("attributes")
    channels = export_info.get("channels")

    for batch_pks in queryset_in_batches(queryset):
        product_batch = Product.objects.filter(pk__in=batch_pks).prefetch_related(
            "attributes",
            "variants",
            "collections",
            "media",
            "product_type",
            "category",
        )

        export_data = get_products_data(
            product_batch, export_fields, attributes, warehouses, channels
        )

        append_to_file(export_data, headers, temporary_file, file_type, delimiter)


def export_gift_cards_in_batches(
        queryset: "QuerySet",
        export_fields: List[str],
        delimiter: str,
        temporary_file: Any,
        file_type: str,
):
    for batch_pks in queryset_in_batches(queryset):
        gift_card_batch = Order.objects.filter(pk__in=batch_pks).prefetch_related(
            "user",
            "shipping_address"
        )

        # Prepare export data
        export_data = []
        for order in gift_card_batch:
            row = {}
            for field in export_fields:
                if field == "number":
                    row[field] = order.number
                elif field == "customer_name":
                    row[field] = order.user.get_full_name() if order.user else "Guest"
                elif field == "address":
                    row[field] = (
                        f"{order.shipping_address.street_address_1}, "
                        f"{order.shipping_address.city}, "
                        f"{order.shipping_address.country}"
                        if order.shipping_address
                        else "No Shipping Address"
                    )
                elif field == "total":
                    row[
                        field] = f"R {order.total.gross.amount:.2f}"  # Assuming `total` has a `gross` attribute
                else:
                    row[field] = ""  # Handle unexpected fields
            export_data.append(row)

        append_to_file(export_data, export_fields, temporary_file, file_type, delimiter)


def queryset_in_batches(queryset):
    """Slice a queryset into batches.

    Input queryset should be sorted be pk.
    """
    start_pk = 0

    while True:
        qs = queryset.order_by("pk").filter(pk__gt=start_pk)[:BATCH_SIZE]
        pks = list(qs.values_list("pk", flat=True))

        if not pks:
            break

        yield pks

        start_pk = pks[-1]


def append_to_file(
        export_data: List[Dict[str, Union[str, bool]]],
        headers: List[str],
        temporary_file: Any,
        file_type: str,
        delimiter: str,
):
    table = etl.fromdicts(export_data, header=headers, missing=" ")

    if file_type == FileTypes.CSV:
        etl.io.csv.appendcsv(table, temporary_file.name, delimiter=delimiter)
    else:
        etl.io.xlsx.appendxlsx(table, temporary_file.name)


def save_csv_file_in_export_file(
        export_file: "ExportFile", temporary_file: IO[bytes], file_name: str
):
    print("fileName: ", file_name)
    print("\n")
    export_file.content_file.save(file_name, temporary_file)
