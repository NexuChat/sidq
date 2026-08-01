# Mutation Benchmark

Labelled mutations: 20666. Labels are the fixture engine's verdicts; generator
intent is retained only for comparison. This is a deterministic regression
consistency corpus, not external or human-labelled accuracy evidence. The compact
complete guard artifact and its provenance are documented in
[`data/benchmark/README.md`](../data/benchmark/README.md).

## Confusion table

| generator intent | BLOCK | WARN | PASS | error |
|---|---|---|---|---|
| harmful | 9209 | 34 | 2623 | 0 |
| benign | 6700 | 0 | 2100 | 0 |
| unknown | 0 | 0 | 0 | 0 |

## How much of this is distinct

**20,666 mutations, 2,114 distinct diffs.** 18,552 rows (89.8%) repeat a diff that appears elsewhere in the corpus, because the generator is asked for a fixed count per family across a small set of models. Every rate below is over the row count; the distinct count is what the evidence actually weighs.

## What this corpus could exercise

The generator's `intent` is not ground truth; it is a hypothesis about a
mutation, and the label is the engine's verdict. A rate below is a measure
of the gate only where the gate could have fired at all.

- **10,089 of 20,666 rows (49%) have no
  downstream lineage in the fixtures.** Every rule that needs a blast
  radius — `pii_exposure`, `critical_downstream`, `wide_blast_radius` — is
  unreachable on those rows by construction, not by omission.
- PII tags exist in these fixtures only on the legacy showcase model, so the
  `expose_pii_tagged_column` family is largely a test of resolution rather
  than of PII detection. Its rate belongs in that light.

Rules that actually fired:

| rule | times fired |
|---|---|
| `critical_downstream` | 10577 |
| `unknown_field` | 9361 |
| `informational` | 8789 |
| `doc_rot` | 2564 |
| `pii_exposure` | 1367 |
| `unresolved_asset` | 1300 |
| `wide_blast_radius` | 1138 |
| `unparseable_sql` | 73 |

## Misses

These are harmful-intent mutations that the engine passed. This is the published miss count.

Count: 2623

| family | misses | generated | rate |
|---|---|---|---|
| `drop_selected_column` | 818 | 2000 | 41% |
| `rename_selected_column` | 805 | 2000 | 40% |
| `expose_pii_tagged_column` | 400 | 1000 | 40% |
| `change_column_type_cast` | 300 | 2000 | 15% |
| `replace_explicit_select_with_star` | 200 | 2000 | 10% |
| `change_aggregation_grain` | 100 | 300 | 33% |

Showing 12 of 2623 diffs, sampled round-robin across families so every family that appears above is represented. The full set is in `data/benchmark/labelled.jsonl`.

### change_aggregation_grain:models/marts/product_performance.sql:0000 (change_aggregation_grain)

```diff
--- a/models/marts/product_performance.sql
+++ b/models/marts/product_performance.sql
@@ -1,23 +1,24 @@
--- depends_on: {{ ref('stg_products') }}
--- depends_on: {{ ref('stg_categories') }}
--- depends_on: {{ ref('stg_order_items') }}
-select
-    products.product_id,
-    products.sku,
-    products.product_name,
-    categories.category_name,
-    categories.department,
-    products.list_price,
-    count(order_items.item_id)::bigint as items_in_orders,
-    coalesce(sum(order_items.quantity), 0)::bigint as units_sold,
-    coalesce(sum(order_items.quantity * order_items.unit_price), 0)::numeric(14, 2) as gross_item_revenue
-from staging.stg_products as products
-join staging.stg_categories as categories on categories.category_id = products.category_id
-left join staging.stg_order_items as order_items on order_items.sku = products.sku
-group by
-    products.product_id,
-    products.sku,
-    products.product_name,
-    categories.category_name,
-    categories.department,
-    products.list_price
+/* depends_on: {{ ref('stg_products') }} */
+/* depends_on: {{ ref('stg_categories') }} */
+/* depends_on: {{ ref('stg_order_items') }} */
+SELECT
+  products.product_id,
+  products.sku,
+  products.product_name,
+  categories.category_name,
+  categories.department,
+  products.list_price,
+  CAST(COUNT(order_items.item_id) AS BIGINT) AS items_in_orders,
+  CAST(COALESCE(SUM(order_items.quantity), 0) AS BIGINT) AS units_sold,
+  CAST(COALESCE(SUM(order_items.quantity * order_items.unit_price), 0) AS DECIMAL(14, 2)) AS gross_item_revenue
+FROM staging.stg_products AS products
+JOIN staging.stg_categories AS categories
+  ON categories.category_id = products.category_id
+LEFT JOIN staging.stg_order_items AS order_items
+  ON order_items.sku = products.sku
+GROUP BY
+  products.product_id,
+  products.sku,
+  products.product_name,
+  categories.category_name,
+  categories.department

```

### change_column_type_cast:models/marts/order_funnel.sql:0000 (change_column_type_cast)

```diff
--- a/models/marts/order_funnel.sql
+++ b/models/marts/order_funnel.sql
@@ -1,20 +1,23 @@
--- depends_on: {{ ref('int_order_enriched') }}
-select
-    order_id,
-    customer_id,
-    customer_email,
-    customer_country,
-    ordered_at,
-    order_status,
-    payment_status,
-    shipment_status,
-    order_total,
-    refund_amount,
-    net_revenue,
-    case
-        when payment_status = 'succeeded' and shipment_status = 'delivered' then 'completed'
-        when payment_status = 'pending' then 'payment_pending'
-        when payment_status = 'refunded' then 'refunded'
-        else 'in_progress'
-    end as funnel_stage
-from intermediate.int_order_enriched
+/* depends_on: {{ ref('int_order_enriched') }} */
+SELECT
+  order_id,
+  customer_id,
+  CAST(customer_email AS BIGINT) AS customer_email,
+  customer_country,
+  ordered_at,
+  order_status,
+  payment_status,
+  shipment_status,
+  order_total,
+  refund_amount,
+  net_revenue,
+  CASE
+    WHEN payment_status = 'succeeded' AND shipment_status = 'delivered'
+    THEN 'completed'
+    WHEN payment_status = 'pending'
+    THEN 'payment_pending'
+    WHEN payment_status = 'refunded'
+    THEN 'refunded'
+    ELSE 'in_progress'
+  END AS funnel_stage
+FROM intermediate.int_order_enriched

```

### change_column_type_cast:models/marts/product_performance.sql:0000 (change_column_type_cast)

```diff
--- a/models/marts/product_performance.sql
+++ b/models/marts/product_performance.sql
@@ -1,23 +1,25 @@
--- depends_on: {{ ref('stg_products') }}
--- depends_on: {{ ref('stg_categories') }}
--- depends_on: {{ ref('stg_order_items') }}
-select
-    products.product_id,
-    products.sku,
-    products.product_name,
-    categories.category_name,
-    categories.department,
-    products.list_price,
-    count(order_items.item_id)::bigint as items_in_orders,
-    coalesce(sum(order_items.quantity), 0)::bigint as units_sold,
-    coalesce(sum(order_items.quantity * order_items.unit_price), 0)::numeric(14, 2) as gross_item_revenue
-from staging.stg_products as products
-join staging.stg_categories as categories on categories.category_id = products.category_id
-left join staging.stg_order_items as order_items on order_items.sku = products.sku
-group by
-    products.product_id,
-    products.sku,
-    products.product_name,
-    categories.category_name,
-    categories.department,
-    products.list_price
+/* depends_on: {{ ref('stg_products') }} */
+/* depends_on: {{ ref('stg_categories') }} */
+/* depends_on: {{ ref('stg_order_items') }} */
+SELECT
+  products.product_id,
+  products.sku,
+  products.product_name,
+  CAST(categories.category_name AS TEXT) AS category_name,
+  categories.department,
+  products.list_price,
+  CAST(COUNT(order_items.item_id) AS BIGINT) AS items_in_orders,
+  CAST(COALESCE(SUM(order_items.quantity), 0) AS BIGINT) AS units_sold,
+  CAST(COALESCE(SUM(order_items.quantity * order_items.unit_price), 0) AS DECIMAL(14, 2)) AS gross_item_revenue
+FROM staging.stg_products AS products
+JOIN staging.stg_categories AS categories
+  ON categories.category_id = products.category_id
+LEFT JOIN staging.stg_order_items AS order_items
+  ON order_items.sku = products.sku
+GROUP BY
+  products.product_id,
+  products.sku,
+  products.product_name,
+  categories.category_name,
+  categories.department,
+  products.list_price

```

### change_column_type_cast:models/order_entry/customers.sql:0000 (change_column_type_cast)

```diff
--- a/models/order_entry/customers.sql
+++ b/models/order_entry/customers.sql
@@ -1,25 +1,25 @@
--- Baseline mirror of the showcase customer model used by the sealed demo PRs.
-select
-    account_mgr_id,
-    address_line1,
-    address_line2,
-    address_line3,
-    country_id,
-    credit_limit,
-    cust_email,
-    cust_first_name,
-    cust_last_name,
-    customer_class,
-    customer_id,
-    customer_since,
-    dob,
-    mailshot,
-    nls_language,
-    nls_territory,
-    partner_mailshot,
-    phone_number,
-    region_id,
-    suggestions,
-    town_city,
-    zipcode
-from b2fd91.order_entry_db.order_entry.customers
+/* Baseline mirror of the showcase customer model used by the sealed demo PRs. */
+SELECT
+  account_mgr_id,
+  address_line1,
+  address_line2,
+  address_line3,
+  country_id,
+  credit_limit,
+  cust_email,
+  cust_first_name,
+  CAST(cust_last_name AS BIGINT) AS cust_last_name,
+  customer_class,
+  customer_id,
+  customer_since,
+  dob,
+  mailshot,
+  nls_language,
+  nls_territory,
+  partner_mailshot,
+  phone_number,
+  region_id,
+  suggestions,
+  town_city,
+  zipcode
+FROM b2fd91.order_entry_db.order_entry.customers

```

### drop_selected_column:models/marts/customer_360.sql:0000 (drop_selected_column)

```diff
--- a/models/marts/customer_360.sql
+++ b/models/marts/customer_360.sql
@@ -1,19 +1,18 @@
--- depends_on: {{ ref('int_customer_lifetime') }}
-select
-    customer_id,
-    customer_email,
-    customer_name,
-    customer_country,
-    customer_created_at,
-    lifetime_order_count,
-    first_order_at,
-    last_order_at,
-    lifetime_order_value,
-    current_plan_name,
-    subscription_status,
-    monthly_amount,
-    session_count,
-    last_session_at,
-    support_ticket_count,
-    unresolved_ticket_count
-from intermediate.int_customer_lifetime
+/* depends_on: {{ ref('int_customer_lifetime') }} */
+SELECT
+  customer_id,
+  customer_email,
+  customer_name,
+  customer_country,
+  customer_created_at,
+  lifetime_order_count,
+  last_order_at,
+  lifetime_order_value,
+  current_plan_name,
+  subscription_status,
+  monthly_amount,
+  session_count,
+  last_session_at,
+  support_ticket_count,
+  unresolved_ticket_count
+FROM intermediate.int_customer_lifetime

```

### drop_selected_column:models/marts/order_funnel.sql:0000 (drop_selected_column)

```diff
--- a/models/marts/order_funnel.sql
+++ b/models/marts/order_funnel.sql
@@ -1,20 +1,14 @@
--- depends_on: {{ ref('int_order_enriched') }}
-select
-    order_id,
-    customer_id,
-    customer_email,
-    customer_country,
-    ordered_at,
-    order_status,
-    payment_status,
-    shipment_status,
-    order_total,
-    refund_amount,
-    net_revenue,
-    case
-        when payment_status = 'succeeded' and shipment_status = 'delivered' then 'completed'
-        when payment_status = 'pending' then 'payment_pending'
-        when payment_status = 'refunded' then 'refunded'
-        else 'in_progress'
-    end as funnel_stage
-from intermediate.int_order_enriched
+/* depends_on: {{ ref('int_order_enriched') }} */
+SELECT
+  order_id,
+  customer_id,
+  customer_email,
+  customer_country,
+  ordered_at,
+  order_status,
+  payment_status,
+  shipment_status,
+  order_total,
+  refund_amount,
+  net_revenue
+FROM intermediate.int_order_enriched

```

### drop_selected_column:models/marts/product_performance.sql:0000 (drop_selected_column)

```diff
--- a/models/marts/product_performance.sql
+++ b/models/marts/product_performance.sql
@@ -1,23 +1,24 @@
--- depends_on: {{ ref('stg_products') }}
--- depends_on: {{ ref('stg_categories') }}
--- depends_on: {{ ref('stg_order_items') }}
-select
-    products.product_id,
-    products.sku,
-    products.product_name,
-    categories.category_name,
-    categories.department,
-    products.list_price,
-    count(order_items.item_id)::bigint as items_in_orders,
-    coalesce(sum(order_items.quantity), 0)::bigint as units_sold,
-    coalesce(sum(order_items.quantity * order_items.unit_price), 0)::numeric(14, 2) as gross_item_revenue
-from staging.stg_products as products
-join staging.stg_categories as categories on categories.category_id = products.category_id
-left join staging.stg_order_items as order_items on order_items.sku = products.sku
-group by
-    products.product_id,
-    products.sku,
-    products.product_name,
-    categories.category_name,
-    categories.department,
-    products.list_price
+/* depends_on: {{ ref('stg_products') }} */
+/* depends_on: {{ ref('stg_categories') }} */
+/* depends_on: {{ ref('stg_order_items') }} */
+SELECT
+  products.product_id,
+  products.sku,
+  products.product_name,
+  categories.category_name,
+  categories.department,
+  products.list_price,
+  CAST(COUNT(order_items.item_id) AS BIGINT) AS items_in_orders,
+  CAST(COALESCE(SUM(order_items.quantity), 0) AS BIGINT) AS units_sold
+FROM staging.stg_products AS products
+JOIN staging.stg_categories AS categories
+  ON categories.category_id = products.category_id
+LEFT JOIN staging.stg_order_items AS order_items
+  ON order_items.sku = products.sku
+GROUP BY
+  products.product_id,
+  products.sku,
+  products.product_name,
+  categories.category_name,
+  categories.department,
+  products.list_price

```

### drop_selected_column:models/order_entry/customers.sql:0000 (drop_selected_column)

```diff
--- a/models/order_entry/customers.sql
+++ b/models/order_entry/customers.sql
@@ -1,25 +1,24 @@
--- Baseline mirror of the showcase customer model used by the sealed demo PRs.
-select
-    account_mgr_id,
-    address_line1,
-    address_line2,
-    address_line3,
-    country_id,
-    credit_limit,
-    cust_email,
-    cust_first_name,
-    cust_last_name,
-    customer_class,
-    customer_id,
-    customer_since,
-    dob,
-    mailshot,
-    nls_language,
-    nls_territory,
-    partner_mailshot,
-    phone_number,
-    region_id,
-    suggestions,
-    town_city,
-    zipcode
-from b2fd91.order_entry_db.order_entry.customers
+/* Baseline mirror of the showcase customer model used by the sealed demo PRs. */
+SELECT
+  account_mgr_id,
+  address_line1,
+  address_line2,
+  address_line3,
+  country_id,
+  credit_limit,
+  cust_email,
+  cust_first_name,
+  cust_last_name,
+  customer_class,
+  customer_id,
+  customer_since,
+  dob,
+  mailshot,
+  nls_language,
+  nls_territory,
+  partner_mailshot,
+  phone_number,
+  region_id,
+  town_city,
+  zipcode
+FROM b2fd91.order_entry_db.order_entry.customers

```

### drop_selected_column:models/staging/stg_categories.sql:0000 (drop_selected_column)

```diff
--- a/models/staging/stg_categories.sql
+++ b/models/staging/stg_categories.sql
@@ -1,9 +1,8 @@
--- depends_on: {{ source('raw', 'categories') }}
-select
-    category_id,
-    category_code,
-    category_name,
-    department,
-    is_active,
-    created_at
-from raw.categories
+/* depends_on: {{ source('raw', 'categories') }} */
+SELECT
+  category_id,
+  category_code,
+  category_name,
+  department,
+  is_active
+FROM raw.categories

```

### drop_selected_column:models/staging/stg_order_items.sql:0000 (drop_selected_column)

```diff
--- a/models/staging/stg_order_items.sql
+++ b/models/staging/stg_order_items.sql
@@ -1,8 +1,7 @@
--- depends_on: {{ source('raw', 'order_items') }}
-select
-    item_id,
-    order_id,
-    sku,
-    quantity::integer as quantity,
-    unit_price::numeric(10, 2) as unit_price
-from raw.order_items
+/* depends_on: {{ source('raw', 'order_items') }} */
+SELECT
+  item_id,
+  order_id,
+  sku,
+  CAST(quantity AS INT) AS quantity
+FROM raw.order_items

```

### drop_selected_column:models/staging/stg_products.sql:0002 (drop_selected_column)

```diff
--- a/models/staging/stg_products.sql
+++ b/models/staging/stg_products.sql
@@ -1,11 +1,10 @@
--- depends_on: {{ source('raw', 'products') }}
-select
-    product_id,
-    category_id,
-    sku,
-    product_name,
-    unit_cost::numeric(10, 2) as unit_cost,
-    list_price::numeric(10, 2) as list_price,
-    is_active,
-    launched_at
-from raw.products
+/* depends_on: {{ source('raw', 'products') }} */
+SELECT
+  product_id,
+  category_id,
+  sku,
+  product_name,
+  CAST(unit_cost AS DECIMAL(10, 2)) AS unit_cost,
+  CAST(list_price AS DECIMAL(10, 2)) AS list_price,
+  launched_at
+FROM raw.products

```

### drop_selected_column:models/staging/stg_refunds.sql:0000 (drop_selected_column)

```diff
--- a/models/staging/stg_refunds.sql
+++ b/models/staging/stg_refunds.sql
@@ -1,9 +1,8 @@
--- depends_on: {{ source('raw', 'refunds') }}
-select
-    refund_id,
-    payment_id,
-    order_id,
-    refund_reason,
-    refund_amount::numeric(12, 2) as refund_amount,
-    refunded_at
-from raw.refunds
+/* depends_on: {{ source('raw', 'refunds') }} */
+SELECT
+  payment_id,
+  order_id,
+  refund_reason,
+  CAST(refund_amount AS DECIMAL(12, 2)) AS refund_amount,
+  refunded_at
+FROM raw.refunds

```

## False alarms

Count: 6700

| family | false alarmes | generated | rate |
|---|---|---|---|
| `add_or_remove_sql_comment` | 1700 | 2000 | 85% |
| `reformat_whitespace` | 1700 | 2000 | 85% |
| `reorder_select_list` | 1700 | 2000 | 85% |
| `add_non_pii_derived_column` | 900 | 2000 | 45% |
| `rename_local_alias` | 400 | 500 | 80% |
| `rename_cte` | 300 | 300 | 100% |

Showing 12 of 6700 diffs, sampled round-robin across families so every family that appears above is represented. The full set is in `data/benchmark/labelled.jsonl`.

### add_non_pii_derived_column:models/customer_revenue.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/customer_revenue.sql
+++ b/models/customer_revenue.sql
@@ -1,21 +1,21 @@
-select
-    customers.customer_id,
-    customers.email,
-    customers.full_name,
-    customers.country,
-    min(orders.ordered_at) as first_order_at,
-    max(orders.ordered_at) as last_order_at,
-    count(distinct orders.order_id)::bigint as order_count,
-    sum(order_items.quantity)::bigint as items_purchased,
-    sum(order_items.quantity * order_items.unit_price)::numeric(14, 2)
-        as lifetime_revenue
-from warehouse.raw.customers as customers
-join warehouse.raw.orders as orders
-    on orders.customer_id = customers.customer_id
-join warehouse.raw.order_items as order_items
-    on order_items.order_id = orders.order_id
-group by
-    customers.customer_id,
-    customers.email,
-    customers.full_name,
-    customers.country
+SELECT
+  customers.customer_id,
+  customers.email,
+  customers.full_name,
+  customers.country,
+  MIN(orders.ordered_at) AS first_order_at,
+  MAX(orders.ordered_at) AS last_order_at,
+  CAST(COUNT(DISTINCT orders.order_id) AS BIGINT) AS order_count,
+  CAST(SUM(order_items.quantity) AS BIGINT) AS items_purchased,
+  CAST(SUM(order_items.quantity * order_items.unit_price) AS DECIMAL(14, 2)) AS lifetime_revenue,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM warehouse.raw.customers AS customers
+JOIN warehouse.raw.orders AS orders
+  ON orders.customer_id = customers.customer_id
+JOIN warehouse.raw.order_items AS order_items
+  ON order_items.order_id = orders.order_id
+GROUP BY
+  customers.customer_id,
+  customers.email,
+  customers.full_name,
+  customers.country

```

### add_non_pii_derived_column:models/intermediate/int_customer_lifetime.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/intermediate/int_customer_lifetime.sql
+++ b/models/intermediate/int_customer_lifetime.sql
@@ -1,53 +1,60 @@
--- depends_on: {{ ref('stg_orders') }}
--- depends_on: {{ ref('stg_web_sessions') }}
--- depends_on: {{ ref('stg_support_tickets') }}
--- depends_on: {{ ref('stg_customers') }}
--- depends_on: {{ ref('stg_subscriptions') }}
-with order_metrics as (
-    select
-        customer_id,
-        count(order_id)::bigint as lifetime_order_count,
-        min(ordered_at) as first_order_at,
-        max(ordered_at) as last_order_at,
-        sum(order_total)::numeric(14, 2) as lifetime_order_value
-    from staging.stg_orders
-    group by customer_id
-),
-session_metrics as (
-    select
-        customer_id,
-        count(session_id)::bigint as session_count,
-        max(session_started_at) as last_session_at
-    from staging.stg_web_sessions
-    group by customer_id
-),
-ticket_metrics as (
-    select
-        customer_id,
-        count(ticket_id)::bigint as support_ticket_count,
-        count(ticket_id) filter (where ticket_status <> 'resolved')::bigint as unresolved_ticket_count
-    from staging.stg_support_tickets
-    group by customer_id
+/* depends_on: {{ ref('stg_orders') }} */
+/* depends_on: {{ ref('stg_web_sessions') }} */
+/* depends_on: {{ ref('stg_support_tickets') }} */
+/* depends_on: {{ ref('stg_customers') }} */
+/* depends_on: {{ ref('stg_subscriptions') }} */
+WITH order_metrics AS (
+  SELECT
+    customer_id,
+    CAST(COUNT(order_id) AS BIGINT) AS lifetime_order_count,
+    MIN(ordered_at) AS first_order_at,
+    MAX(ordered_at) AS last_order_at,
+    CAST(SUM(order_total) AS DECIMAL(14, 2)) AS lifetime_order_value
+  FROM staging.stg_orders
+  GROUP BY
+    customer_id
+), session_metrics AS (
+  SELECT
+    customer_id,
+    CAST(COUNT(session_id) AS BIGINT) AS session_count,
+    MAX(session_started_at) AS last_session_at
+  FROM staging.stg_web_sessions
+  GROUP BY
+    customer_id
+), ticket_metrics AS (
+  SELECT
+    customer_id,
+    CAST(COUNT(ticket_id) AS BIGINT) AS support_ticket_count,
+    CAST(COUNT(ticket_id) FILTER(WHERE
+      ticket_status <> 'resolved') AS BIGINT) AS unresolved_ticket_count
+  FROM staging.stg_support_tickets
+  GROUP BY
+    customer_id
 )
-select
-    customers.customer_id,
-    customers.customer_email,
-    customers.customer_name,
-    customers.customer_country,
-    customers.customer_created_at,
-    coalesce(order_metrics.lifetime_order_count, 0)::bigint as lifetime_order_count,
-    order_metrics.first_order_at,
-    order_metrics.last_order_at,
-    coalesce(order_metrics.lifetime_order_value, 0)::numeric(14, 2) as lifetime_order_value,
-    subscriptions.plan_name as current_plan_name,
-    subscriptions.subscription_status,
-    subscriptions.monthly_amount,
-    coalesce(session_metrics.session_count, 0)::bigint as session_count,
-    session_metrics.last_session_at,
-    coalesce(ticket_metrics.support_ticket_count, 0)::bigint as support_ticket_count,
-    coalesce(ticket_metrics.unresolved_ticket_count, 0)::bigint as unresolved_ticket_count
-from staging.stg_customers as customers
-left join order_metrics on order_metrics.customer_id = customers.customer_id
-left join staging.stg_subscriptions as subscriptions on subscriptions.customer_id = customers.customer_id
-left join session_metrics on session_metrics.customer_id = customers.customer_id
-left join ticket_metrics on ticket_metrics.customer_id = customers.customer_id
+SELECT
+  customers.customer_id,
+  customers.customer_email,
+  customers.customer_name,
+  customers.customer_country,
+  customers.customer_created_at,
+  CAST(COALESCE(order_metrics.lifetime_order_count, 0) AS BIGINT) AS lifetime_order_count,
+  order_metrics.first_order_at,
+  order_metrics.last_order_at,
+  CAST(COALESCE(order_metrics.lifetime_order_value, 0) AS DECIMAL(14, 2)) AS lifetime_order_value,
+  subscriptions.plan_name AS current_plan_name,
+  subscriptions.subscription_status,
+  subscriptions.monthly_amount,
+  CAST(COALESCE(session_metrics.session_count, 0) AS BIGINT) AS session_count,
+  session_metrics.last_session_at,
+  CAST(COALESCE(ticket_metrics.support_ticket_count, 0) AS BIGINT) AS support_ticket_count,
+  CAST(COALESCE(ticket_metrics.unresolved_ticket_count, 0) AS BIGINT) AS unresolved_ticket_count,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM staging.stg_customers AS customers
+LEFT JOIN order_metrics
+  ON order_metrics.customer_id = customers.customer_id
+LEFT JOIN staging.stg_subscriptions AS subscriptions
+  ON subscriptions.customer_id = customers.customer_id
+LEFT JOIN session_metrics
+  ON session_metrics.customer_id = customers.customer_id
+LEFT JOIN ticket_metrics
+  ON ticket_metrics.customer_id = customers.customer_id

```

### add_non_pii_derived_column:models/intermediate/int_order_enriched.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/intermediate/int_order_enriched.sql
+++ b/models/intermediate/int_order_enriched.sql
@@ -1,47 +1,56 @@
--- depends_on: {{ ref('stg_order_items') }}
--- depends_on: {{ ref('stg_refunds') }}
--- depends_on: {{ ref('stg_orders') }}
--- depends_on: {{ ref('stg_customers') }}
--- depends_on: {{ ref('stg_payments') }}
--- depends_on: {{ ref('stg_shipments') }}
-with item_totals as (
-    select
-        order_id,
-        sum(quantity)::bigint as item_quantity,
-        sum(quantity * unit_price)::numeric(14, 2) as item_gross_revenue
-    from staging.stg_order_items
-    group by order_id
-),
-refund_totals as (
-    select
-        order_id,
-        sum(refund_amount)::numeric(12, 2) as refund_amount
-    from staging.stg_refunds
-    group by order_id
+/* depends_on: {{ ref('stg_order_items') }} */
+/* depends_on: {{ ref('stg_refunds') }} */
+/* depends_on: {{ ref('stg_orders') }} */
+/* depends_on: {{ ref('stg_customers') }} */
+/* depends_on: {{ ref('stg_payments') }} */
+/* depends_on: {{ ref('stg_shipments') }} */
+WITH item_totals AS (
+  SELECT
+    order_id,
+    CAST(SUM(quantity) AS BIGINT) AS item_quantity,
+    CAST(SUM(quantity * unit_price) AS DECIMAL(14, 2)) AS item_gross_revenue
+  FROM staging.stg_order_items
+  GROUP BY
+    order_id
+), refund_totals AS (
+  SELECT
+    order_id,
+    CAST(SUM(refund_amount) AS DECIMAL(12, 2)) AS refund_amount
+  FROM staging.stg_refunds
+  GROUP BY
+    order_id
 )
-select
-    orders.order_id,
-    orders.customer_id,
-    customers.customer_email,
-    customers.customer_name,
-    customers.customer_country,
-    orders.order_status,
-    orders.ordered_at,
-    orders.order_total,
-    item_totals.item_quantity,
-    item_totals.item_gross_revenue,
-    payments.payment_id,
-    payments.payment_provider,
-    payments.payment_status,
-    payments.payment_amount,
-    shipments.shipment_status,
-    shipments.carrier,
-    shipments.delivered_at,
-    coalesce(refund_totals.refund_amount, 0)::numeric(12, 2) as refund_amount,
-    (orders.order_total - coalesce(refund_totals.refund_amount, 0))::numeric(12, 2) as net_revenue
-from staging.stg_orders as orders
-join staging.stg_customers as customers on customers.customer_id = orders.customer_id
-left join item_totals on item_totals.order_id = orders.order_id
-left join staging.stg_payments as payments on payments.order_id = orders.order_id
-left join staging.stg_shipments as shipments on shipments.order_id = orders.order_id
-left join refund_totals on refund_totals.order_id = orders.order_id
+SELECT
+  orders.order_id,
+  orders.customer_id,
+  customers.customer_email,
+  customers.customer_name,
+  customers.customer_country,
+  orders.order_status,
+  orders.ordered_at,
+  orders.order_total,
+  item_totals.item_quantity,
+  item_totals.item_gross_revenue,
+  payments.payment_id,
+  payments.payment_provider,
+  payments.payment_status,
+  payments.payment_amount,
+  shipments.shipment_status,
+  shipments.carrier,
+  shipments.delivered_at,
+  CAST(COALESCE(refund_totals.refund_amount, 0) AS DECIMAL(12, 2)) AS refund_amount,
+  CAST((
+    orders.order_total - COALESCE(refund_totals.refund_amount, 0)
+  ) AS DECIMAL(12, 2)) AS net_revenue,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM staging.stg_orders AS orders
+JOIN staging.stg_customers AS customers
+  ON customers.customer_id = orders.customer_id
+LEFT JOIN item_totals
+  ON item_totals.order_id = orders.order_id
+LEFT JOIN staging.stg_payments AS payments
+  ON payments.order_id = orders.order_id
+LEFT JOIN staging.stg_shipments AS shipments
+  ON shipments.order_id = orders.order_id
+LEFT JOIN refund_totals
+  ON refund_totals.order_id = orders.order_id

```

### add_non_pii_derived_column:models/intermediate/int_payment_reconciliation.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/intermediate/int_payment_reconciliation.sql
+++ b/models/intermediate/int_payment_reconciliation.sql
@@ -1,35 +1,46 @@
--- depends_on: {{ ref('stg_refunds') }}
--- depends_on: {{ ref('stg_payments') }}
--- depends_on: {{ ref('stg_orders') }}
--- depends_on: {{ ref('stg_customers') }}
-with refund_totals as (
-    select
-        payment_id,
-        sum(refund_amount)::numeric(12, 2) as refunded_amount
-    from staging.stg_refunds
-    group by payment_id
+/* depends_on: {{ ref('stg_refunds') }} */
+/* depends_on: {{ ref('stg_payments') }} */
+/* depends_on: {{ ref('stg_orders') }} */
+/* depends_on: {{ ref('stg_customers') }} */
+WITH refund_totals AS (
+  SELECT
+    payment_id,
+    CAST(SUM(refund_amount) AS DECIMAL(12, 2)) AS refunded_amount
+  FROM staging.stg_refunds
+  GROUP BY
+    payment_id
 )
-select
-    payments.payment_id,
-    payments.order_id,
-    payments.customer_id,
-    customers.customer_email,
-    payments.provider_transaction_id,
-    payments.payment_provider,
-    payments.payment_status,
-    payments.payment_amount,
-    orders.order_total,
-    coalesce(refund_totals.refunded_amount, 0)::numeric(12, 2) as refunded_amount,
-    (payments.payment_amount - coalesce(refund_totals.refunded_amount, 0))::numeric(12, 2) as settled_amount,
-    payments.paid_at,
-    payments.payment_created_at,
-    case
-        when payments.payment_status = 'succeeded' and payments.payment_amount = orders.order_total then 'reconciled'
-        when payments.payment_status = 'pending' then 'awaiting_payment'
-        when payments.payment_status = 'refunded' then 'refunded'
-        else 'investigate'
-    end as reconciliation_status
-from staging.stg_payments as payments
-join staging.stg_orders as orders on orders.order_id = payments.order_id
-join staging.stg_customers as customers on customers.customer_id = payments.customer_id
-left join refund_totals on refund_totals.payment_id = payments.payment_id
+SELECT
+  payments.payment_id,
+  payments.order_id,
+  payments.customer_id,
+  customers.customer_email,
+  payments.provider_transaction_id,
+  payments.payment_provider,
+  payments.payment_status,
+  payments.payment_amount,
+  orders.order_total,
+  CAST(COALESCE(refund_totals.refunded_amount, 0) AS DECIMAL(12, 2)) AS refunded_amount,
+  CAST((
+    payments.payment_amount - COALESCE(refund_totals.refunded_amount, 0)
+  ) AS DECIMAL(12, 2)) AS settled_amount,
+  payments.paid_at,
+  payments.payment_created_at,
+  CASE
+    WHEN payments.payment_status = 'succeeded'
+    AND payments.payment_amount = orders.order_total
+    THEN 'reconciled'
+    WHEN payments.payment_status = 'pending'
+    THEN 'awaiting_payment'
+    WHEN payments.payment_status = 'refunded'
+    THEN 'refunded'
+    ELSE 'investigate'
+  END AS reconciliation_status,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM staging.stg_payments AS payments
+JOIN staging.stg_orders AS orders
+  ON orders.order_id = payments.order_id
+JOIN staging.stg_customers AS customers
+  ON customers.customer_id = payments.customer_id
+LEFT JOIN refund_totals
+  ON refund_totals.payment_id = payments.payment_id

```

### add_non_pii_derived_column:models/marts/revenue_daily.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/marts/revenue_daily.sql
+++ b/models/marts/revenue_daily.sql
@@ -1,15 +1,17 @@
--- depends_on: {{ ref('int_order_enriched') }}
-select
-    ordered_at::date as revenue_date,
-    customer_country,
-    payment_provider,
-    count(order_id)::bigint as order_count,
-    count(order_id) filter (where payment_status = 'succeeded')::bigint as paid_order_count,
-    sum(order_total)::numeric(14, 2) as gross_revenue,
-    sum(refund_amount)::numeric(14, 2) as refunded_revenue,
-    sum(net_revenue)::numeric(14, 2) as net_revenue
-from intermediate.int_order_enriched
-group by
-    ordered_at::date,
-    customer_country,
-    payment_provider
+/* depends_on: {{ ref('int_order_enriched') }} */
+SELECT
+  CAST(ordered_at AS DATE) AS revenue_date,
+  customer_country,
+  payment_provider,
+  CAST(COUNT(order_id) AS BIGINT) AS order_count,
+  CAST(COUNT(order_id) FILTER(WHERE
+    payment_status = 'succeeded') AS BIGINT) AS paid_order_count,
+  CAST(SUM(order_total) AS DECIMAL(14, 2)) AS gross_revenue,
+  CAST(SUM(refund_amount) AS DECIMAL(14, 2)) AS refunded_revenue,
+  CAST(SUM(net_revenue) AS DECIMAL(14, 2)) AS net_revenue,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM intermediate.int_order_enriched
+GROUP BY
+  CAST(ordered_at AS DATE),
+  customer_country,
+  payment_provider

```

### add_non_pii_derived_column:models/staging/stg_customers.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/staging/stg_customers.sql
+++ b/models/staging/stg_customers.sql
@@ -1,8 +1,9 @@
--- depends_on: {{ source('raw', 'customers') }}
-select
-    customer_id,
-    email as customer_email,
-    full_name as customer_name,
-    country as customer_country,
-    created_at as customer_created_at
-from raw.customers
+/* depends_on: {{ source('raw', 'customers') }} */
+SELECT
+  customer_id,
+  email AS customer_email,
+  full_name AS customer_name,
+  country AS customer_country,
+  created_at AS customer_created_at,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM raw.customers

```

### add_non_pii_derived_column:models/staging/stg_orders.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/staging/stg_orders.sql
+++ b/models/staging/stg_orders.sql
@@ -1,8 +1,9 @@
--- depends_on: {{ source('raw', 'orders') }}
-select
-    order_id,
-    customer_id,
-    order_total::numeric(12, 2) as order_total,
-    status as order_status,
-    ordered_at
-from raw.orders
+/* depends_on: {{ source('raw', 'orders') }} */
+SELECT
+  order_id,
+  customer_id,
+  CAST(order_total AS DECIMAL(12, 2)) AS order_total,
+  status AS order_status,
+  ordered_at,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM raw.orders

```

### add_non_pii_derived_column:models/staging/stg_payments.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/staging/stg_payments.sql
+++ b/models/staging/stg_payments.sql
@@ -1,18 +1,19 @@
--- depends_on: {{ source('raw', 'payments') }}
-select
-    payment_id,
-    order_id,
-    customer_id,
-    provider as payment_provider,
-    provider_transaction_id,
-    payment_status,
-    amount::numeric(12, 2) as payment_amount,
-    billing_email,
-    billing_phone,
-    billing_address_line1,
-    billing_address_line2,
-    billing_city,
-    billing_country,
-    paid_at,
-    created_at as payment_created_at
-from raw.payments
+/* depends_on: {{ source('raw', 'payments') }} */
+SELECT
+  payment_id,
+  order_id,
+  customer_id,
+  provider AS payment_provider,
+  provider_transaction_id,
+  payment_status,
+  CAST(amount AS DECIMAL(12, 2)) AS payment_amount,
+  billing_email,
+  billing_phone,
+  billing_address_line1,
+  billing_address_line2,
+  billing_city,
+  billing_country,
+  paid_at,
+  created_at AS payment_created_at,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM raw.payments

```

### add_non_pii_derived_column:models/staging/stg_support_tickets.sql:0000 (add_non_pii_derived_column)

```diff
--- a/models/staging/stg_support_tickets.sql
+++ b/models/staging/stg_support_tickets.sql
@@ -1,11 +1,12 @@
--- depends_on: {{ source('raw', 'support_tickets') }}
-select
-    ticket_id,
-    customer_id,
-    order_id,
-    ticket_status,
-    priority as ticket_priority,
-    subject,
-    opened_at,
-    resolved_at
-from raw.support_tickets
+/* depends_on: {{ source('raw', 'support_tickets') }} */
+SELECT
+  ticket_id,
+  customer_id,
+  order_id,
+  ticket_status,
+  priority AS ticket_priority,
+  subject,
+  opened_at,
+  resolved_at,
+  CAST(1 AS INT) AS sidq_benchmark_flag
+FROM raw.support_tickets

```

### add_or_remove_sql_comment:models/customer_revenue.sql:0000 (add_or_remove_sql_comment)

```diff
--- a/models/customer_revenue.sql
+++ b/models/customer_revenue.sql
@@ -1,3 +1,4 @@
+-- sidq-benchmark: metadata-only comment
 select
     customers.customer_id,
     customers.email,

```

### add_or_remove_sql_comment:models/intermediate/int_customer_lifetime.sql:0000 (add_or_remove_sql_comment)

```diff
--- a/models/intermediate/int_customer_lifetime.sql
+++ b/models/intermediate/int_customer_lifetime.sql
@@ -1,3 +1,4 @@
+-- sidq-benchmark: metadata-only comment
 -- depends_on: {{ ref('stg_orders') }}
 -- depends_on: {{ ref('stg_web_sessions') }}
 -- depends_on: {{ ref('stg_support_tickets') }}

```

### add_or_remove_sql_comment:models/intermediate/int_order_enriched.sql:0000 (add_or_remove_sql_comment)

```diff
--- a/models/intermediate/int_order_enriched.sql
+++ b/models/intermediate/int_order_enriched.sql
@@ -1,3 +1,4 @@
+-- sidq-benchmark: metadata-only comment
 -- depends_on: {{ ref('stg_order_items') }}
 -- depends_on: {{ ref('stg_refunds') }}
 -- depends_on: {{ ref('stg_orders') }}

```

## Per-family breakdown

| family | BLOCK | WARN | PASS | error |
|---|---|---|---|---|
| add_non_pii_derived_column | 900 | 0 | 1100 | 0 |
| add_or_remove_sql_comment | 1700 | 0 | 300 | 0 |
| change_aggregation_grain | 200 | 0 | 100 | 0 |
| change_column_type_cast | 1700 | 0 | 300 | 0 |
| change_join_key | 466 | 0 | 0 | 0 |
| delete_where_filter | 100 | 0 | 0 | 0 |
| drop_selected_column | 1166 | 16 | 818 | 0 |
| expose_pii_tagged_column | 600 | 0 | 400 | 0 |
| reference_nonexistent_upstream_column | 2000 | 0 | 0 | 0 |
| reformat_whitespace | 1700 | 0 | 300 | 0 |
| rename_cte | 300 | 0 | 0 | 0 |
| rename_local_alias | 400 | 0 | 100 | 0 |
| rename_selected_column | 1177 | 18 | 805 | 0 |
| reorder_select_list | 1700 | 0 | 300 | 0 |
| replace_explicit_select_with_star | 1800 | 0 | 200 | 0 |

Not applicable to this demo corpus:

- `delete_where_filter` — no demo model has a WHERE filter to delete.
- `rename_cte` — no demo model contains a CTE to rename.

## Limitations

The generator is our own, so it tests the gate against our imagination, not against the real world. It is useful for reproducible regression measurement, but it cannot establish real-world coverage.
