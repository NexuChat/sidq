\set ON_ERROR_STOP on

CREATE SCHEMA raw;
CREATE SCHEMA analytics;

CREATE TABLE raw.customers (
    customer_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email text NOT NULL UNIQUE,
    full_name text NOT NULL,
    country text NOT NULL,
    created_at timestamptz NOT NULL
);

COMMENT ON TABLE raw.customers IS
    'Canonical customer records loaded from the commerce application.';
COMMENT ON COLUMN raw.customers.email IS
    'PII: customer email address used for lifecycle reporting.';

CREATE TABLE raw.orders (
    order_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL,
    order_total numeric(12, 2) NOT NULL DEFAULT 0
        CONSTRAINT orders_order_total_check CHECK (order_total >= 0),
    status text NOT NULL
        CONSTRAINT orders_status_check CHECK (status IN ('pending', 'paid', 'fulfilled', 'refunded')),
    CONSTRAINT orders_customer_id_fkey
        FOREIGN KEY (customer_id) REFERENCES raw.customers (customer_id),
    ordered_at timestamptz NOT NULL
);

CREATE INDEX orders_customer_ordered_at_idx
    ON raw.orders (customer_id, ordered_at DESC);
CREATE INDEX orders_status_ordered_at_idx
    ON raw.orders (status, ordered_at DESC);

CREATE TABLE raw.order_items (
    item_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id bigint NOT NULL REFERENCES raw.orders (order_id),
    sku text NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(10, 2) NOT NULL CHECK (unit_price >= 0)
);

CREATE INDEX order_items_order_id_idx ON raw.order_items (order_id);
CREATE INDEX order_items_sku_idx ON raw.order_items (sku);

INSERT INTO raw.customers (email, full_name, country, created_at)
VALUES
    ('amina.haddad@example.com', 'Amina Haddad', 'AE', '2025-01-04 09:15:00+00'),
    ('omar.khalil@example.com', 'Omar Khalil', 'SA', '2025-01-09 14:22:00+00'),
    ('layla.mansour@example.com', 'Layla Mansour', 'EG', '2025-01-14 08:05:00+00'),
    ('youssef.nasser@example.com', 'Youssef Nasser', 'JO', '2025-01-19 16:42:00+00'),
    ('noura.saleh@example.com', 'Noura Saleh', 'KW', '2025-01-25 11:18:00+00'),
    ('karim.fawzi@example.com', 'Karim Fawzi', 'EG', '2025-02-01 13:37:00+00'),
    ('sara.rahman@example.com', 'Sara Rahman', 'QA', '2025-02-07 10:11:00+00'),
    ('tariq.aziz@example.com', 'Tariq Aziz', 'BH', '2025-02-13 18:24:00+00'),
    ('mariam.said@example.com', 'Mariam Said', 'OM', '2025-02-18 07:56:00+00'),
    ('faisal.hassan@example.com', 'Faisal Hassan', 'SA', '2025-02-24 12:43:00+00'),
    ('dana.sharif@example.com', 'Dana Sharif', 'LB', '2025-03-02 15:19:00+00'),
    ('samir.abbas@example.com', 'Samir Abbas', 'AE', '2025-03-08 09:48:00+00'),
    ('hana.ismail@example.com', 'Hana Ismail', 'EG', '2025-03-13 17:06:00+00'),
    ('zayd.hamdan@example.com', 'Zayd Hamdan', 'JO', '2025-03-19 10:34:00+00'),
    ('reem.qasim@example.com', 'Reem Qasim', 'QA', '2025-03-25 14:58:00+00'),
    ('ali.darwish@example.com', 'Ali Darwish', 'LB', '2025-04-01 08:29:00+00'),
    ('salma.adel@example.com', 'Salma Adel', 'EG', '2025-04-06 19:12:00+00'),
    ('mazen.farid@example.com', 'Mazen Farid', 'AE', '2025-04-12 11:03:00+00'),
    ('lina.najjar@example.com', 'Lina Najjar', 'JO', '2025-04-18 16:27:00+00'),
    ('rami.salem@example.com', 'Rami Salem', 'KW', '2025-04-23 09:41:00+00'),
    ('yasmin.hakim@example.com', 'Yasmin Hakim', 'OM', '2025-04-29 13:16:00+00'),
    ('bilal.saad@example.com', 'Bilal Saad', 'BH', '2025-05-05 07:52:00+00'),
    ('farah.amin@example.com', 'Farah Amin', 'SA', '2025-05-11 18:09:00+00'),
    ('adel.rashid@example.com', 'Adel Rashid', 'AE', '2025-05-16 10:46:00+00'),
    ('rania.taha@example.com', 'Rania Taha', 'EG', '2025-05-22 15:31:00+00'),
    ('khaled.jaber@example.com', 'Khaled Jaber', 'JO', '2025-05-28 08:14:00+00'),
    ('samar.noor@example.com', 'Samar Noor', 'QA', '2025-06-03 12:38:00+00'),
    ('hadi.karam@example.com', 'Hadi Karam', 'LB', '2025-06-09 17:23:00+00'),
    ('dalia.wael@example.com', 'Dalia Wael', 'KW', '2025-06-15 09:07:00+00'),
    ('jad.nasr@example.com', 'Jad Nasr', 'AE', '2025-06-21 14:49:00+00'),
    ('mona.latif@example.com', 'Mona Latif', 'OM', '2025-06-27 11:26:00+00'),
    ('waleed.sami@example.com', 'Waleed Sami', 'BH', '2025-07-03 16:04:00+00'),
    ('ghada.anwar@example.com', 'Ghada Anwar', 'SA', '2025-07-09 08:33:00+00'),
    ('nabil.maher@example.com', 'Nabil Maher', 'EG', '2025-07-15 13:55:00+00'),
    ('hiba.zaki@example.com', 'Hiba Zaki', 'JO', '2025-07-21 10:17:00+00'),
    ('imad.bashir@example.com', 'Imad Bashir', 'AE', '2025-07-27 18:41:00+00');

INSERT INTO raw.orders (customer_id, status, ordered_at)
SELECT
    ((order_number * 7 - 1) % 36) + 1,
    (ARRAY['paid', 'fulfilled', 'fulfilled', 'paid', 'pending', 'refunded'])
        [((order_number - 1) % 6) + 1],
    '2026-01-03 08:00:00+00'::timestamptz
        + (order_number * interval '2 days')
        + ((order_number % 9) * interval '1 hour')
FROM generate_series(1, 72) AS generated(order_number);

INSERT INTO raw.order_items (order_id, sku, quantity, unit_price)
SELECT
    orders.order_id,
    (ARRAY[
        'HOME-LAMP-01',
        'TECH-CHARGER-02',
        'KITCHEN-MUG-03',
        'OFFICE-NOTEBOOK-04',
        'TRAVEL-BAG-05',
        'AUDIO-HEADPHONES-06',
        'FITNESS-BOTTLE-07',
        'HOME-CUSHION-08',
        'TECH-STAND-09',
        'BEAUTY-SET-10'
    ])[((orders.order_id + item_number - 2) % 10) + 1],
    ((orders.order_id + item_number) % 3 + 1)::integer,
    (ARRAY[
        29.90,
        44.50,
        12.75,
        8.90,
        64.00,
        89.95,
        18.25,
        34.80,
        27.40,
        52.60
    ])[((orders.order_id + item_number - 2) % 10) + 1]::numeric(10, 2)
FROM raw.orders AS orders
CROSS JOIN generate_series(1, 2) AS generated(item_number);

UPDATE raw.orders AS orders
SET order_total = totals.order_total
FROM (
    SELECT
        order_id,
        sum(quantity * unit_price)::numeric(12, 2) AS order_total
    FROM raw.order_items
    GROUP BY order_id
) AS totals
WHERE totals.order_id = orders.order_id;

CREATE VIEW analytics.customer_revenue AS
SELECT
    customers.customer_id,
    customers.email,
    customers.full_name,
    customers.country,
    min(orders.ordered_at) AS first_order_at,
    max(orders.ordered_at) AS last_order_at,
    count(DISTINCT orders.order_id)::bigint AS order_count,
    sum(order_items.quantity)::bigint AS items_purchased,
    sum(order_items.quantity * order_items.unit_price)::numeric(14, 2)
        AS lifetime_revenue
FROM raw.customers AS customers
JOIN raw.orders AS orders
    ON orders.customer_id = customers.customer_id
JOIN raw.order_items AS order_items
    ON order_items.order_id = orders.order_id
GROUP BY
    customers.customer_id,
    customers.email,
    customers.full_name,
    customers.country;

COMMENT ON VIEW analytics.customer_revenue IS
    'Customer-level commerce rollup used by retention and lifecycle reporting.';

-- The remaining tables deliberately resemble the operational sources a small
-- commerce team would receive.  They are kept in raw so dbt owns all reporting
-- joins and transformations.
CREATE TABLE raw.categories (
    category_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_code text NOT NULL UNIQUE,
    category_name text NOT NULL,
    department text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL,
    CONSTRAINT categories_name_department_unique UNIQUE (category_name, department)
);

CREATE TABLE raw.products (
    product_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_id bigint NOT NULL REFERENCES raw.categories (category_id),
    sku text NOT NULL UNIQUE,
    product_name text NOT NULL,
    unit_cost numeric(10, 2) NOT NULL CHECK (unit_cost >= 0),
    list_price numeric(10, 2) NOT NULL CHECK (list_price > 0),
    is_active boolean NOT NULL DEFAULT true,
    launched_at timestamptz NOT NULL,
    CONSTRAINT products_margin_check CHECK (list_price >= unit_cost)
);

CREATE TABLE raw.payments (
    payment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id bigint NOT NULL REFERENCES raw.orders (order_id),
    customer_id bigint NOT NULL REFERENCES raw.customers (customer_id),
    provider text NOT NULL CHECK (provider IN ('card', 'wallet', 'bank_transfer')),
    provider_transaction_id text NOT NULL UNIQUE,
    payment_status text NOT NULL CHECK (payment_status IN ('pending', 'succeeded', 'refunded')),
    amount numeric(12, 2) NOT NULL CHECK (amount >= 0),
    billing_email text NOT NULL,
    billing_phone text NOT NULL,
    billing_address_line1 text NOT NULL,
    billing_address_line2 text,
    billing_city text NOT NULL,
    billing_country text NOT NULL,
    paid_at timestamptz,
    created_at timestamptz NOT NULL,
    CONSTRAINT payments_one_per_order UNIQUE (order_id, provider),
    CONSTRAINT payments_status_timestamp_check CHECK (
        (payment_status = 'succeeded' AND paid_at IS NOT NULL)
        OR (payment_status IN ('pending', 'refunded'))
    )
);

CREATE TABLE raw.shipments (
    shipment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id bigint NOT NULL UNIQUE REFERENCES raw.orders (order_id),
    carrier text NOT NULL CHECK (carrier IN ('DHL', 'Aramex', 'UPS')),
    tracking_number text NOT NULL UNIQUE,
    shipment_status text NOT NULL CHECK (shipment_status IN ('label_created', 'in_transit', 'delivered')),
    recipient_phone text NOT NULL,
    shipping_address_line1 text NOT NULL,
    shipping_address_line2 text,
    shipping_city text NOT NULL,
    shipping_country text NOT NULL,
    shipped_at timestamptz,
    delivered_at timestamptz,
    CONSTRAINT shipments_delivery_timing_check CHECK (
        delivered_at IS NULL OR (shipped_at IS NOT NULL AND delivered_at >= shipped_at)
    )
);

CREATE TABLE raw.refunds (
    refund_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id bigint NOT NULL REFERENCES raw.payments (payment_id),
    order_id bigint NOT NULL REFERENCES raw.orders (order_id),
    refund_reason text NOT NULL CHECK (refund_reason IN ('customer_request', 'damaged', 'fraud_review')),
    refund_amount numeric(12, 2) NOT NULL CHECK (refund_amount > 0),
    refunded_at timestamptz NOT NULL,
    CONSTRAINT refunds_payment_order_unique UNIQUE (payment_id, order_id)
);

CREATE TABLE raw.subscriptions (
    subscription_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES raw.customers (customer_id),
    plan_name text NOT NULL CHECK (plan_name IN ('starter', 'plus', 'pro')),
    subscription_status text NOT NULL CHECK (subscription_status IN ('active', 'paused', 'cancelled')),
    monthly_amount numeric(10, 2) NOT NULL CHECK (monthly_amount > 0),
    started_at timestamptz NOT NULL,
    cancelled_at timestamptz,
    CONSTRAINT subscriptions_customer_plan_start_unique UNIQUE (customer_id, plan_name, started_at),
    CONSTRAINT subscriptions_cancelled_timing_check CHECK (
        cancelled_at IS NULL OR cancelled_at >= started_at
    )
);

CREATE TABLE raw.web_sessions (
    session_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint REFERENCES raw.customers (customer_id),
    anonymous_id text NOT NULL,
    session_started_at timestamptz NOT NULL,
    session_ended_at timestamptz NOT NULL,
    landing_channel text NOT NULL CHECK (landing_channel IN ('organic', 'paid_search', 'email', 'social', 'direct')),
    device_type text NOT NULL CHECK (device_type IN ('desktop', 'mobile', 'tablet')),
    ip_address inet NOT NULL,
    converted_order_id bigint REFERENCES raw.orders (order_id),
    CONSTRAINT sessions_customer_start_unique UNIQUE (customer_id, session_started_at),
    CONSTRAINT sessions_duration_check CHECK (session_ended_at >= session_started_at)
);

CREATE TABLE raw.support_tickets (
    ticket_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES raw.customers (customer_id),
    order_id bigint REFERENCES raw.orders (order_id),
    ticket_status text NOT NULL CHECK (ticket_status IN ('open', 'pending', 'resolved')),
    priority text NOT NULL CHECK (priority IN ('low', 'normal', 'high')),
    subject text NOT NULL,
    opened_at timestamptz NOT NULL,
    resolved_at timestamptz,
    CONSTRAINT tickets_customer_opened_unique UNIQUE (customer_id, opened_at),
    CONSTRAINT tickets_resolution_timing_check CHECK (
        resolved_at IS NULL OR resolved_at >= opened_at
    )
);

CREATE INDEX products_category_id_idx ON raw.products (category_id);
CREATE INDEX payments_customer_created_at_idx ON raw.payments (customer_id, created_at DESC);
CREATE INDEX shipments_order_id_idx ON raw.shipments (order_id);
CREATE INDEX refunds_order_id_idx ON raw.refunds (order_id);
CREATE INDEX subscriptions_customer_id_idx ON raw.subscriptions (customer_id);
CREATE INDEX web_sessions_customer_started_at_idx ON raw.web_sessions (customer_id, session_started_at DESC);
CREATE INDEX support_tickets_customer_id_idx ON raw.support_tickets (customer_id);

INSERT INTO raw.categories (category_code, category_name, department, is_active, created_at)
SELECT
    format('CAT-%02s', category_number),
    format('Commerce category %s', category_number),
    (ARRAY['home', 'technology', 'kitchen', 'office', 'travel', 'audio'])[((category_number - 1) % 6) + 1],
    category_number <= 28,
    '2024-01-01 00:00:00+00'::timestamptz + (category_number * interval '3 days')
FROM generate_series(1, 30) AS generated(category_number);

INSERT INTO raw.products (category_id, sku, product_name, unit_cost, list_price, is_active, launched_at)
SELECT
    ((product_number - 1) % 30) + 1,
    CASE product_number
        WHEN 1 THEN 'HOME-LAMP-01'
        WHEN 2 THEN 'TECH-CHARGER-02'
        WHEN 3 THEN 'KITCHEN-MUG-03'
        WHEN 4 THEN 'OFFICE-NOTEBOOK-04'
        WHEN 5 THEN 'TRAVEL-BAG-05'
        WHEN 6 THEN 'AUDIO-HEADPHONES-06'
        WHEN 7 THEN 'FITNESS-BOTTLE-07'
        WHEN 8 THEN 'HOME-CUSHION-08'
        WHEN 9 THEN 'TECH-STAND-09'
        WHEN 10 THEN 'BEAUTY-SET-10'
        ELSE format('PROD-%03s', product_number)
    END,
    format('Commerce product %s', product_number),
    (8 + product_number * 1.15)::numeric(10, 2),
    (16 + product_number * 2.35)::numeric(10, 2),
    product_number <= 56,
    '2024-02-01 00:00:00+00'::timestamptz + (product_number * interval '4 days')
FROM generate_series(1, 60) AS generated(product_number);

INSERT INTO raw.payments (
    order_id, customer_id, provider, provider_transaction_id, payment_status, amount,
    billing_email, billing_phone, billing_address_line1, billing_address_line2,
    billing_city, billing_country, paid_at, created_at
)
SELECT
    orders.order_id,
    orders.customer_id,
    (ARRAY['card', 'wallet', 'bank_transfer'])[((orders.order_id - 1) % 3) + 1],
    format('txn_%06s', orders.order_id),
    CASE orders.status
        WHEN 'pending' THEN 'pending'
        WHEN 'refunded' THEN 'refunded'
        ELSE 'succeeded'
    END,
    orders.order_total,
    customers.email,
    format('+971-50-%04s', 1000 + customers.customer_id),
    format('%s Market Street', customers.customer_id),
    CASE WHEN customers.customer_id % 4 = 0 THEN format('Apartment %s', customers.customer_id) END,
    (ARRAY['Dubai', 'Riyadh', 'Cairo', 'Amman', 'Doha', 'Manama'])[((customers.customer_id - 1) % 6) + 1],
    customers.country,
    CASE WHEN orders.status IN ('paid', 'fulfilled') THEN orders.ordered_at + interval '15 minutes' END,
    orders.ordered_at
FROM raw.orders AS orders
JOIN raw.customers AS customers ON customers.customer_id = orders.customer_id;

INSERT INTO raw.shipments (
    order_id, carrier, tracking_number, shipment_status, recipient_phone,
    shipping_address_line1, shipping_address_line2, shipping_city, shipping_country,
    shipped_at, delivered_at
)
SELECT
    orders.order_id,
    (ARRAY['DHL', 'Aramex', 'UPS'])[((orders.order_id - 1) % 3) + 1],
    format('SHIP-%06s', orders.order_id),
    CASE WHEN orders.status IN ('fulfilled', 'refunded') THEN 'delivered' ELSE 'label_created' END,
    format('+971-55-%04s', 2000 + customers.customer_id),
    format('%s Harbour Road', customers.customer_id),
    CASE WHEN customers.customer_id % 5 = 0 THEN format('Suite %s', customers.customer_id) END,
    (ARRAY['Dubai', 'Riyadh', 'Cairo', 'Amman', 'Doha', 'Manama'])[((customers.customer_id - 1) % 6) + 1],
    customers.country,
    CASE WHEN orders.status IN ('fulfilled', 'refunded') THEN orders.ordered_at + interval '1 day' END,
    CASE WHEN orders.status IN ('fulfilled', 'refunded') THEN orders.ordered_at + interval '5 days' END
FROM raw.orders AS orders
JOIN raw.customers AS customers ON customers.customer_id = orders.customer_id;

INSERT INTO raw.refunds (payment_id, order_id, refund_reason, refund_amount, refunded_at)
SELECT
    payments.payment_id,
    payments.order_id,
    (ARRAY['customer_request', 'damaged', 'fraud_review'])[((payments.payment_id - 1) % 3) + 1],
    CASE WHEN orders.status = 'refunded' THEN payments.amount ELSE (payments.amount / 2)::numeric(12, 2) END,
    orders.ordered_at + interval '10 days'
FROM raw.payments AS payments
JOIN raw.orders AS orders ON orders.order_id = payments.order_id
WHERE orders.status IN ('fulfilled', 'refunded');

INSERT INTO raw.subscriptions (customer_id, plan_name, subscription_status, monthly_amount, started_at, cancelled_at)
SELECT
    customers.customer_id,
    (ARRAY['starter', 'plus', 'pro'])[((customers.customer_id - 1) % 3) + 1],
    CASE WHEN customers.customer_id % 7 = 0 THEN 'cancelled' WHEN customers.customer_id % 5 = 0 THEN 'paused' ELSE 'active' END,
    (ARRAY[12.00, 29.00, 79.00])[((customers.customer_id - 1) % 3) + 1],
    customers.created_at + interval '14 days',
    CASE WHEN customers.customer_id % 7 = 0 THEN customers.created_at + interval '120 days' END
FROM raw.customers AS customers;

INSERT INTO raw.web_sessions (
    customer_id, anonymous_id, session_started_at, session_ended_at, landing_channel,
    device_type, ip_address, converted_order_id
)
SELECT
    ((session_number * 5 - 1) % 36) + 1,
    format('anon_%05s', session_number),
    '2026-01-01 07:00:00+00'::timestamptz + (session_number * interval '19 hours'),
    '2026-01-01 07:00:00+00'::timestamptz + (session_number * interval '19 hours') + interval '8 minutes',
    (ARRAY['organic', 'paid_search', 'email', 'social', 'direct'])[((session_number - 1) % 5) + 1],
    (ARRAY['desktop', 'mobile', 'tablet'])[((session_number - 1) % 3) + 1],
    format('198.51.100.%s', ((session_number - 1) % 200) + 1)::inet,
    CASE WHEN session_number % 3 = 0 THEN session_number END
FROM generate_series(1, 72) AS generated(session_number);

INSERT INTO raw.support_tickets (customer_id, order_id, ticket_status, priority, subject, opened_at, resolved_at)
SELECT
    customers.customer_id,
    ((customers.customer_id * 7 - 1) % 72) + 1,
    CASE WHEN customers.customer_id % 4 = 0 THEN 'open' WHEN customers.customer_id % 3 = 0 THEN 'pending' ELSE 'resolved' END,
    (ARRAY['low', 'normal', 'high'])[((customers.customer_id - 1) % 3) + 1],
    format('Commerce support request %s', customers.customer_id),
    customers.created_at + interval '45 days',
    CASE WHEN customers.customer_id % 4 = 0 OR customers.customer_id % 3 = 0 THEN NULL ELSE customers.created_at + interval '47 days' END
FROM raw.customers AS customers;
