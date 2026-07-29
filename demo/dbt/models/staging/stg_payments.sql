-- depends_on: {{ source('raw', 'payments') }}
select
    payment_id,
    order_id,
    customer_id,
    provider as payment_provider,
    provider_transaction_id,
    payment_status,
    amount::numeric(12, 2) as payment_amount,
    billing_email,
    billing_phone,
    billing_address_line1,
    billing_address_line2,
    billing_city,
    billing_country,
    paid_at,
    created_at as payment_created_at
from raw.payments
