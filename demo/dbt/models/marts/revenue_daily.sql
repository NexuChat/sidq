-- depends_on: {{ ref('int_order_enriched') }}
select
    ordered_at::date as revenue_date,
    customer_country,
    payment_provider,
    count(order_id)::bigint as order_count,
    count(order_id) filter (where payment_status = 'succeeded')::bigint as paid_order_count,
    sum(order_total)::numeric(14, 2) as gross_revenue,
    sum(refund_amount)::numeric(14, 2) as refunded_revenue,
    sum(net_revenue)::numeric(14, 2) as net_revenue
from intermediate.int_order_enriched
group by
    ordered_at::date,
    customer_country,
    payment_provider
