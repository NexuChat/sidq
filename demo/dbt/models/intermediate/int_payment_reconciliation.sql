-- depends_on: {{ ref('stg_refunds') }}
-- depends_on: {{ ref('stg_payments') }}
-- depends_on: {{ ref('stg_orders') }}
-- depends_on: {{ ref('stg_customers') }}
with refund_totals as (
    select
        payment_id,
        sum(refund_amount)::numeric(12, 2) as refunded_amount
    from staging.stg_refunds
    group by payment_id
)
select
    payments.payment_id,
    payments.order_id,
    payments.customer_id,
    customers.customer_email,
    payments.provider_transaction_id,
    payments.payment_provider,
    payments.payment_status,
    payments.payment_amount,
    orders.order_total,
    coalesce(refund_totals.refunded_amount, 0)::numeric(12, 2) as refunded_amount,
    (payments.payment_amount - coalesce(refund_totals.refunded_amount, 0))::numeric(12, 2) as settled_amount,
    payments.paid_at,
    payments.payment_created_at,
    case
        when payments.payment_status = 'succeeded' and payments.payment_amount = orders.order_total then 'reconciled'
        when payments.payment_status = 'pending' then 'awaiting_payment'
        when payments.payment_status = 'refunded' then 'refunded'
        else 'investigate'
    end as reconciliation_status
from staging.stg_payments as payments
join staging.stg_orders as orders on orders.order_id = payments.order_id
join staging.stg_customers as customers on customers.customer_id = payments.customer_id
left join refund_totals on refund_totals.payment_id = payments.payment_id
