-- depends_on: {{ source('raw', 'support_tickets') }}
select
    ticket_id,
    customer_id,
    order_id,
    ticket_status,
    priority as ticket_priority,
    subject,
    opened_at,
    resolved_at
from raw.support_tickets
