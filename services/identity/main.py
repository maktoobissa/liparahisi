import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException

from backend.main import (
    auth_status,
    change_password,
    confirm_password_reset,
    connect_db,
    login,
    logout,
    register_seller,
    register_customer,
    request_password_reset,
    setup_admin,
    verify_email,
)


def verify_gateway_token(
    x_identity_service_token: str | None = Header(default=None),
):
    expected = os.getenv("IDENTITY_SERVICE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="IDENTITY_SERVICE_TOKEN haijawekwa.",
        )
    if not x_identity_service_token or not secrets.compare_digest(
        x_identity_service_token,
        expected,
    ):
        raise HTTPException(status_code=401, detail="Ombi la ndani halijaidhinishwa.")


app = FastAPI(title="LipaRahisi Identity API")


@app.get("/health")
def health():
    with connect_db() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok"}


app.add_api_route(
    "/status",
    auth_status,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/setup",
    setup_admin,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/register",
    register_seller,
    methods=["POST"],
    status_code=201,
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/customer/register",
    register_customer,
    methods=["POST"],
    status_code=201,
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/login",
    login,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/logout",
    logout,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/password",
    change_password,
    methods=["PUT"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/password-reset/request",
    request_password_reset,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/password-reset/confirm",
    confirm_password_reset,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/verify-email",
    verify_email,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
