-- depends_on: {{ source('raw', 'subscriptions') }}
select
    subscription_id,
    customer_id,
    plan_name,
    subscription_status,
    monthly_amount::numeric(10, 2) as monthly_amount,
    started_at,
    cancelled_at
from raw.subscriptions
