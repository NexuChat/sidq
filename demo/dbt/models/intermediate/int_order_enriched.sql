-- depends_on: {{ ref('stg_order_items') }}
-- depends_on: {{ ref('stg_refunds') }}
-- depends_on: {{ ref('stg_orders') }}
-- depends_on: {{ ref('stg_customers') }}
-- depends_on: {{ ref('stg_payments') }}
-- depends_on: {{ ref('stg_shipments') }}
with item_totals as (
    select
        order_id,
        sum(quantity)::bigint as item_quantity,
        sum(quantity * unit_price)::numeric(14, 2) as item_gross_revenue
    from staging.stg_order_items
    group by order_id
),
refund_totals as (
    select
        order_id,
        sum(refund_amount)::numeric(12, 2) as refund_amount
    from staging.stg_refunds
    group by order_id
)
select
    orders.order_id,
    orders.customer_id,
    customers.customer_email,
    customers.customer_name,
    customers.customer_country,
    orders.order_status,
    orders.ordered_at,
    orders.order_total,
    item_totals.item_quantity,
    item_totals.item_gross_revenue,
    payments.payment_id,
    payments.payment_provider,
    payments.payment_status,
    payments.payment_amount,
    shipments.shipment_status,
    shipments.carrier,
    shipments.delivered_at,
    coalesce(refund_totals.refund_amount, 0)::numeric(12, 2) as refund_amount,
    (orders.order_total - coalesce(refund_totals.refund_amount, 0))::numeric(12, 2) as net_revenue
from staging.stg_orders as orders
join staging.stg_customers as customers on customers.customer_id = orders.customer_id
left join item_totals on item_totals.order_id = orders.order_id
left join staging.stg_payments as payments on payments.order_id = orders.order_id
left join staging.stg_shipments as shipments on shipments.order_id = orders.order_id
left join refund_totals on refund_totals.order_id = orders.order_id
