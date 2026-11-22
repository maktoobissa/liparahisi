import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException

from backend.main import (
    CancellationInput,
    CancellationResolutionInput,
    DeliveryUpdateInput,
    ReviewInput,
    archive_sale,
    complete_cart_checkout,
    create_cart_checkout,
    create_checkout,
    create_review,
    dashboard,
    demo_complete,
    list_cancellations,
    list_sales,
    list_reviews,
    order_status,
    order_history,
    moderate_review,
    process_clickpesa_webhook,
    process_pesapal_callback,
    process_pesapal_webhook,
    read_cart_checkout,
    request_cancellation,
    resolve_cancellation,
    update_delivery,
    connect_db,
)


def verify_gateway_token(
    x_orders_service_token: str | None = Header(default=None),
):
    expected = os.getenv("ORDERS_SERVICE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ORDERS_SERVICE_TOKEN haijawekwa.",
        )
    if not x_orders_service_token or not secrets.compare_digest(
        x_orders_service_token,
        expected,
    ):
        raise HTTPException(status_code=401, detail="Ombi la ndani halijaidhinishwa.")


app = FastAPI(title="LipaRahisi Orders API")


@app.get("/health")
def health():
    with connect_db() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok"}


app.add_api_route(
    "/products/{product_id}/checkout",
    create_checkout,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/cart/checkout",
    create_cart_checkout,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/webhooks/clickpesa",
    process_clickpesa_webhook,
    methods=["POST"],
)
app.add_api_route(
    "/webhooks/pesapal",
    process_pesapal_webhook,
    methods=["GET", "POST"],
)
app.add_api_route(
    "/callbacks/pesapal",
    process_pesapal_callback,
    methods=["GET"],
)
app.add_api_route(
    "/checkouts/{checkout_id}",
    read_cart_checkout,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/checkouts/{checkout_id}/demo-complete",
    complete_cart_checkout,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/orders/history",
    order_history,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/orders/{order_id}",
    order_status,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/orders/{order_id}/delivery",
    update_delivery,
    methods=["PUT"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/orders/{order_id}/cancellation",
    request_cancellation,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/orders/{order_id}/products/{product_id}/review",
    create_review,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/cancellations",
    list_cancellations,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/cancellations/{cancellation_id}",
    resolve_cancellation,
    methods=["PATCH"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/reviews",
    list_reviews,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/reviews/{review_id}",
    moderate_review,
    methods=["PATCH"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/orders/{order_id}/demo-complete",
    demo_complete,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/dashboard",
    dashboard,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/sales",
    list_sales,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/sales/{order_id}/archive",
    archive_sale,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
