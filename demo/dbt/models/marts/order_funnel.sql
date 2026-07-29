-- depends_on: {{ ref('int_order_enriched') }}
select
    order_id,
    customer_id,
    customer_email,
    customer_country,
    ordered_at,
    order_status,
    payment_status,
    shipment_status,
    order_total,
    refund_amount,
    net_revenue,
    case
        when payment_status = 'succeeded' and shipment_status = 'delivered' then 'completed'
        when payment_status = 'pending' then 'payment_pending'
        when payment_status = 'refunded' then 'refunded'
        else 'in_progress'
    end as funnel_stage
from intermediate.int_order_enriched
