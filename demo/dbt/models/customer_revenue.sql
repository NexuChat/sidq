select
    customers.customer_id,
    customers.email,
    customers.full_name,
    customers.country,
    min(orders.ordered_at) as first_order_at,
    max(orders.ordered_at) as last_order_at,
    count(distinct orders.order_id)::bigint as order_count,
    sum(order_items.quantity)::bigint as items_purchased,
    sum(order_items.quantity * order_items.unit_price)::numeric(14, 2)
        as lifetime_revenue
from warehouse.raw.customers as customers
join warehouse.raw.orders as orders
    on orders.customer_id = customers.customer_id
join warehouse.raw.order_items as order_items
    on order_items.order_id = orders.order_id
group by
    customers.customer_id,
    customers.email,
    customers.full_name,
    customers.country
