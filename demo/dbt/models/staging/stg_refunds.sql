-- depends_on: {{ source('raw', 'refunds') }}
select
    refund_id,
    payment_id,
    order_id,
    refund_reason,
    refund_amount::numeric(12, 2) as refund_amount,
    refunded_at
from raw.refunds
