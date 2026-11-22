import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException

from backend.main import (
    auth_status,
    change_password,
    confirm_password_reset,
    connect_db,
    disable_mfa,
    enable_mfa,
    list_sessions,
    login,
    logout,
    mfa_status,
    register_seller,
    register_customer,
    revoke_other_sessions,
    request_password_reset,
    setup_mfa,
    setup_admin,
    verify_email,
    get_seller_kyc_status,
    get_language_preference,
    update_language_preference,
    submit_seller_kyc,
    delete_seller_kyc_submission,
    list_kyc_requirements,
    LanguagePreferenceInput,
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
    "/language",
    get_language_preference,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/language",
    update_language_preference,
    methods=["PUT"],
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
    "/mfa/status",
    mfa_status,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/mfa/setup",
    setup_mfa,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/mfa/enable",
    enable_mfa,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/mfa/disable",
    disable_mfa,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/sessions",
    list_sessions,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/sessions/revoke-others",
    revoke_other_sessions,
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

app.add_api_route(
    "/kyc",
    get_seller_kyc_status,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)

app.add_api_route(
    "/kyc",
    submit_seller_kyc,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)

app.add_api_route(
    "/kyc",
    delete_seller_kyc_submission,
    methods=["DELETE"],
    dependencies=[Depends(verify_gateway_token)],
)

app.add_api_route(
    "/kyc/requirements",
    list_kyc_requirements,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
