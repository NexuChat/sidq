-- depends_on: {{ ref('stg_orders') }}
-- depends_on: {{ ref('stg_web_sessions') }}
-- depends_on: {{ ref('stg_support_tickets') }}
-- depends_on: {{ ref('stg_customers') }}
-- depends_on: {{ ref('stg_subscriptions') }}
with order_metrics as (
    select
        customer_id,
        count(order_id)::bigint as lifetime_order_count,
        min(ordered_at) as first_order_at,
        max(ordered_at) as last_order_at,
        sum(order_total)::numeric(14, 2) as lifetime_order_value
    from staging.stg_orders
    group by customer_id
),
session_metrics as (
    select
        customer_id,
        count(session_id)::bigint as session_count,
        max(session_started_at) as last_session_at
    from staging.stg_web_sessions
    group by customer_id
),
ticket_metrics as (
    select
        customer_id,
        count(ticket_id)::bigint as support_ticket_count,
        count(ticket_id) filter (where ticket_status <> 'resolved')::bigint as unresolved_ticket_count
    from staging.stg_support_tickets
    group by customer_id
)
select
    customers.customer_id,
    customers.customer_email,
    customers.customer_name,
    customers.customer_country,
    customers.customer_created_at,
    coalesce(order_metrics.lifetime_order_count, 0)::bigint as lifetime_order_count,
    order_metrics.first_order_at,
    order_metrics.last_order_at,
    coalesce(order_metrics.lifetime_order_value, 0)::numeric(14, 2) as lifetime_order_value,
    subscriptions.plan_name as current_plan_name,
    subscriptions.subscription_status,
    subscriptions.monthly_amount,
    coalesce(session_metrics.session_count, 0)::bigint as session_count,
    session_metrics.last_session_at,
    coalesce(ticket_metrics.support_ticket_count, 0)::bigint as support_ticket_count,
    coalesce(ticket_metrics.unresolved_ticket_count, 0)::bigint as unresolved_ticket_count
from staging.stg_customers as customers
left join order_metrics on order_metrics.customer_id = customers.customer_id
left join staging.stg_subscriptions as subscriptions on subscriptions.customer_id = customers.customer_id
left join session_metrics on session_metrics.customer_id = customers.customer_id
left join ticket_metrics on ticket_metrics.customer_id = customers.customer_id
