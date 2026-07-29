-- depends_on: {{ source('raw', 'categories') }}
select
    category_id,
    category_code,
    category_name,
    department,
    is_active,
    created_at
from raw.categories
