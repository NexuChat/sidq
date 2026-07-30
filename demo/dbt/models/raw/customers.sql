-- The live source was renamed without re-ingesting DataHub.
select
    customer_id,
    email_address,
    full_name,
    country,
    created_at
from warehouse.raw.customers
