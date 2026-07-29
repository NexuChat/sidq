-- depends_on: {{ source('raw', 'customers') }}
select
    customer_id,
    email as customer_email,
    full_name as customer_name,
    country as customer_country,
    created_at as customer_created_at
from raw.customers
