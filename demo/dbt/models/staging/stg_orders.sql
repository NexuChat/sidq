-- depends_on: {{ source('raw', 'orders') }}
select
    order_id,
    customer_id,
    order_total::numeric(12, 2) as order_total,
    status as order_status,
    ordered_at
from raw.orders
