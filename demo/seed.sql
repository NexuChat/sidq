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
    customer_id bigint NOT NULL REFERENCES raw.customers (customer_id),
    order_total numeric(12, 2) NOT NULL DEFAULT 0 CHECK (order_total >= 0),
    status text NOT NULL CHECK (status IN ('pending', 'paid', 'fulfilled', 'refunded')),
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
