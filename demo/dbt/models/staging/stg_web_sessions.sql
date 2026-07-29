-- depends_on: {{ source('raw', 'web_sessions') }}
select
    session_id,
    customer_id,
    anonymous_id,
    session_started_at,
    session_ended_at,
    landing_channel,
    device_type,
    ip_address,
    converted_order_id
from raw.web_sessions
-- Zero-duration sessions are bot probes and tracker noise; downstream
-- attribution must never count them.
where session_ended_at > session_started_at
