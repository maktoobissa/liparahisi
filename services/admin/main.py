import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from backend.main import (
    CancellationResolutionInput,
    CouponInput,
    CouponStatusInput,
    ReturnResolutionInput,
    ReviewModerationInput,
    SellerStatusInput,
    SupportTicketUpdateInput,
    SupportTicketInput,
    connect_db,
    acknowledge_system_alert,
    create_coupon,
    create_support_ticket,
    export_sales,
    list_cancellations,
    list_audit,
    list_coupons,
    list_reviews,
    list_sellers,
    list_system_alerts,
    list_return_requests,
    list_support_tickets,
    moderate_review,
    resolve_cancellation,
    resolve_return_request,
    update_seller_status,
    set_coupon_status,
    update_support_ticket,
)


def verify_gateway_token(
    x_admin_service_token: str | None = Header(default=None),
):
    expected = os.getenv("ADMIN_SERVICE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SERVICE_TOKEN haijawekwa.",
        )
    if not x_admin_service_token or not secrets.compare_digest(
        x_admin_service_token,
        expected,
    ):
        raise HTTPException(status_code=401, detail="Ombi la ndani halijaidhinishwa.")


app = FastAPI(title="LipaRahisi Admin API")


@app.get("/health")
def health():
    with connect_db() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/coupons", dependencies=[Depends(verify_gateway_token)])
def coupons(request: Request):
    return list_coupons(request)


@app.post("/coupons", status_code=201, dependencies=[Depends(verify_gateway_token)])
def coupons_create(payload: CouponInput, request: Request):
    return create_coupon(payload, request)


@app.patch(
    "/coupons/{coupon_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def coupons_status(
    coupon_id: str,
    payload: CouponStatusInput,
    request: Request,
):
    return set_coupon_status(coupon_id, payload, request)


@app.get("/sellers", dependencies=[Depends(verify_gateway_token)])
def sellers(request: Request):
    return list_sellers(request)


@app.patch(
    "/sellers/{seller_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def set_seller_status(
    seller_id: str,
    payload: SellerStatusInput,
    request: Request,
):
    return update_seller_status(seller_id, payload, request)


@app.get("/audit", dependencies=[Depends(verify_gateway_token)])
def audit(request: Request):
    return list_audit(request)


@app.get("/system-alerts", dependencies=[Depends(verify_gateway_token)])
def system_alerts(request: Request):
    return list_system_alerts(request)


@app.patch(
    "/system-alerts/{alert_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def system_alert_acknowledge(alert_id: str, request: Request):
    return acknowledge_system_alert(alert_id, request)


@app.get("/export/sales", dependencies=[Depends(verify_gateway_token)])
def sales_export(request: Request):
    return export_sales(request)


@app.get("/cancellations", dependencies=[Depends(verify_gateway_token)])
def cancellation_requests(request: Request):
    return list_cancellations(request)


@app.patch(
    "/cancellations/{cancellation_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def cancellation_decision(
    cancellation_id: str,
    payload: CancellationResolutionInput,
    request: Request,
):
    return resolve_cancellation(cancellation_id, payload, request)


@app.get("/returns", dependencies=[Depends(verify_gateway_token)])
def return_requests(request: Request):
    return list_return_requests(request)


@app.patch(
    "/returns/{return_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def return_decision(
    return_id: str,
    payload: ReturnResolutionInput,
    request: Request,
):
    return resolve_return_request(return_id, payload, request)


@app.get("/reviews", dependencies=[Depends(verify_gateway_token)])
def pending_reviews(request: Request):
    return list_reviews(request)


@app.patch(
    "/reviews/{review_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def review_decision(
    review_id: str,
    payload: ReviewModerationInput,
    request: Request,
):
    return moderate_review(review_id, payload, request)


@app.get("/support-tickets", dependencies=[Depends(verify_gateway_token)])
def support_tickets(request: Request):
    return list_support_tickets(request)


@app.post(
    "/support-tickets",
    status_code=201,
    dependencies=[Depends(verify_gateway_token)],
)
def submit_support_ticket(payload: SupportTicketInput, request: Request):
    return create_support_ticket(payload, request)


@app.patch(
    "/support-tickets/{ticket_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def resolve_support_ticket(
    ticket_id: str,
    payload: SupportTicketUpdateInput,
    request: Request,
):
    return update_support_ticket(ticket_id, payload, request)
