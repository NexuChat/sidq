-- depends_on: {{ source('raw', 'shipments') }}
select
    shipment_id,
    order_id,
    carrier,
    tracking_number,
    shipment_status,
    recipient_phone,
    shipping_address_line1,
    shipping_address_line2,
    shipping_city,
    shipping_country,
    shipped_at,
    delivered_at
from raw.shipments
