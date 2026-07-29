-- depends_on: {{ ref('stg_products') }}
-- depends_on: {{ ref('stg_categories') }}
-- depends_on: {{ ref('stg_order_items') }}
select
    products.product_id,
    products.sku,
    products.product_name,
    categories.category_name,
    categories.department,
    products.list_price,
    count(order_items.item_id)::bigint as items_in_orders,
    coalesce(sum(order_items.quantity), 0)::bigint as units_sold,
    coalesce(sum(order_items.quantity * order_items.unit_price), 0)::numeric(14, 2) as gross_item_revenue
from staging.stg_products as products
join staging.stg_categories as categories on categories.category_id = products.category_id
left join staging.stg_order_items as order_items on order_items.sku = products.sku
group by
    products.product_id,
    products.sku,
    products.product_name,
    categories.category_name,
    categories.department,
    products.list_price
