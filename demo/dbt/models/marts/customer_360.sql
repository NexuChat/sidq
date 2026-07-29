-- depends_on: {{ ref('int_customer_lifetime') }}
select
    customer_id,
    customer_email,
    customer_name,
    customer_country,
    customer_created_at,
    lifetime_order_count,
    first_order_at,
    last_order_at,
    lifetime_order_value,
    current_plan_name,
    subscription_status,
    monthly_amount,
    session_count,
    last_session_at,
    support_ticket_count,
    unresolved_ticket_count
from intermediate.int_customer_lifetime
