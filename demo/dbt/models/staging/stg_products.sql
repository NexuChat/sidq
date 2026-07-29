-- depends_on: {{ source('raw', 'products') }}
select
    product_id,
    category_id,
    sku,
    product_name,
    unit_cost::numeric(10, 2) as unit_cost,
    list_price::numeric(10, 2) as list_price,
    is_active,
    launched_at
from raw.products
