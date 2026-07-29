-- depends_on: {{ source('raw', 'order_items') }}
select
    item_id,
    order_id,
    sku,
    quantity::integer as quantity,
    unit_price::numeric(10, 2) as unit_price
from raw.order_items
