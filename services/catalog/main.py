import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from backend.main import (
    CategoryInput,
    ProductInput,
    ProductCsvInput,
    ProductUpdateInput,
    ProductVariantInput,
    ProductVariantStockInput,
    StorefrontInput,
    add_to_wishlist,
    archive_product,
    connect_db,
    add_product_variant,
    create_category,
    create_product,
    delete_product_image,
    delete_product_gallery_image,
    export_products,
    import_products,
    list_categories,
    list_products,
    list_wishlist,
    low_stock_products,
    product_inventory_movements,
    read_product,
    read_product_image,
    read_product_gallery_image,
    read_storefront,
    remove_from_wishlist,
    update_product,
    update_product_variant_stock,
    update_storefront,
    upload_product_image,
    upload_product_gallery,
)


def verify_gateway_token(
    x_catalog_service_token: str | None = Header(default=None),
):
    expected = os.getenv("CATALOG_SERVICE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="CATALOG_SERVICE_TOKEN haijawekwa.",
        )
    if not x_catalog_service_token or not secrets.compare_digest(
        x_catalog_service_token,
        expected,
    ):
        raise HTTPException(status_code=401, detail="Ombi la ndani halijaidhinishwa.")


app = FastAPI(title="LipaRahisi Catalog API")


@app.get("/health")
def health():
    with connect_db() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/products", dependencies=[Depends(verify_gateway_token)])
def products(
    request: Request,
    q: str = "",
    seller: str = "",
    min_amount: int = 0,
    max_amount: int = 999999999,
    category: str = "",
):
    return list_products(request, q, seller, min_amount, max_amount, category)


@app.get("/categories", dependencies=[Depends(verify_gateway_token)])
def categories():
    return list_categories()


@app.post("/categories", dependencies=[Depends(verify_gateway_token)])
def add_category(payload: CategoryInput, request: Request):
    return create_category(payload, request)


@app.put("/storefront", dependencies=[Depends(verify_gateway_token)])
def edit_storefront(payload: StorefrontInput, request: Request):
    return update_storefront(payload, request)


@app.get("/products/export", dependencies=[Depends(verify_gateway_token)])
def products_export(request: Request):
    return export_products(request)


@app.post("/products/import", dependencies=[Depends(verify_gateway_token)])
def products_import(payload: ProductCsvInput, request: Request):
    return import_products(payload, request)


@app.get("/inventory/low-stock", dependencies=[Depends(verify_gateway_token)])
def low_stock(request: Request):
    return low_stock_products(request)


@app.get(
    "/products/{product_id}/movements",
    dependencies=[Depends(verify_gateway_token)],
)
def stock_movements(product_id: str, request: Request):
    return product_inventory_movements(product_id, request)


@app.patch(
    "/products/{product_id}/variants/{variant_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def variant_stock(
    product_id: str,
    variant_id: str,
    payload: ProductVariantStockInput,
    request: Request,
):
    return update_product_variant_stock(product_id, variant_id, payload, request)


@app.post(
    "/products/{product_id}/variants",
    status_code=201,
    dependencies=[Depends(verify_gateway_token)],
)
def create_variant(
    product_id: str,
    payload: ProductVariantInput,
    request: Request,
):
    return add_product_variant(product_id, payload, request)


app.add_api_route(
    "/products/{product_id}/image",
    upload_product_image,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/products/{product_id}/image",
    read_product_image,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/products/{product_id}/image",
    delete_product_image,
    methods=["DELETE"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/products/{product_id}/gallery",
    upload_product_gallery,
    methods=["POST"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/products/{product_id}/gallery/{image_id}",
    read_product_gallery_image,
    methods=["GET"],
    dependencies=[Depends(verify_gateway_token)],
)
app.add_api_route(
    "/products/{product_id}/gallery/{image_id}",
    delete_product_gallery_image,
    methods=["DELETE"],
    dependencies=[Depends(verify_gateway_token)],
)

@app.get(
    "/storefronts/{store_slug}",
    dependencies=[Depends(verify_gateway_token)],
)
def storefront(store_slug: str):
    return read_storefront(store_slug)


@app.get("/wishlist", dependencies=[Depends(verify_gateway_token)])
def wishlist(request: Request):
    return list_wishlist(request)


@app.post(
    "/wishlist/{product_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def wishlist_add(product_id: str, request: Request):
    return add_to_wishlist(product_id, request)


@app.delete(
    "/wishlist/{product_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def wishlist_remove(product_id: str, request: Request):
    return remove_from_wishlist(product_id, request)


@app.post(
    "/products",
    status_code=201,
    dependencies=[Depends(verify_gateway_token)],
)
def add_product(payload: ProductInput, request: Request):
    return create_product(payload, request)


@app.get("/products/{product_id}", dependencies=[Depends(verify_gateway_token)])
def product(product_id: str):
    return read_product(product_id)


@app.put(
    "/products/{product_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def edit_product(
    product_id: str,
    payload: ProductUpdateInput,
    request: Request,
):
    return update_product(product_id, payload, request)


@app.delete(
    "/products/{product_id}",
    dependencies=[Depends(verify_gateway_token)],
)
def remove_product(product_id: str, request: Request):
    return archive_product(product_id, request)
