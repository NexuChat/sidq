<!-- sidq-pr-bot:sticky -->
# 🚫 BLOCKED — <code>pii_exposure</code>, <code>critical_downstream</code>

> **Provenance: LIVE DATAHUB.** Evidence was read from the live graph.

## Deterministic policy decision

Only the deterministic policy findings in this section affect the merge decision.

### 🚫 <code>pii_exposure</code> — BLOCK

**Why:** PII exposure is not permitted for dbt · order_entry_db.order_entry.customers.cust_email.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers.cust_email</code>](https://datahub.mlki.app/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- Changed column: <code>cust_email</code>
- PII tags: <code>tag · PII_Data</code>
- PII tag not carried by **10 downstream consumers**, including <code>Looker view · order-entry-looker.view.order_details</code>, <code>Looker explore · order-entry.explore.order_details</code>, <code>Power BI · datahub_order_entries.Customer_Analytics_Measures</code>

### 🚫 <code>pii_exposure</code> — BLOCK

**Why:** PII exposure is not permitted for dbt · order_entry_db.order_entry.customers.cust_email.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers.cust_email</code>](https://datahub.mlki.app/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- Changed column: <code>cust_email</code>
- PII tags: <code>tag · PII_Data</code>
- Reaches: <code>Looker dashboard · dashboards.53</code>
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>dbt · ORDER_ENTRY_DB.analytics.order_details.cust_email</code> → <code>Snowflake · order_entry_db.analytics.order_details.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.224</code> → <code>Looker dashboard · dashboards.53</code>
- Path note: Column lineage is proven through the BI field; chart and dashboard hops are entity-level.

### ⚠️ <code>wide_blast_radius</code> — WARN

**Why:** This change affects 16 downstream consumers for dbt · order_entry_db.order_entry.customers.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers</code>](https://datahub.mlki.app/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- PII tags: <code>tag · PII_Data</code>
- Reaches: <code>Looker dashboard · dashboards.53</code>
- Blast radius: **16 downstream consumers** within 3 hops
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>dbt · ORDER_ENTRY_DB.analytics.order_details.cust_email</code> → <code>Snowflake · order_entry_db.analytics.order_details.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.224</code> → <code>Looker dashboard · dashboards.53</code>
- Path note: Column lineage is proven through the BI field; chart and dashboard hops are entity-level.

<details>
<summary>Downstream consumers (16)</summary>

- <code>Looker chart · dashboard_elements.221</code>
- <code>Looker chart · dashboard_elements.222</code>
- <code>Looker chart · dashboard_elements.223</code>
- <code>Looker chart · dashboard_elements.224</code>
- <code>Looker dashboard · dashboards.53</code>
- <code>Looker explore · order-entry.explore.order_details</code>
- <code>Looker view · order-entry-looker.view.order_details</code>
- <code>Power BI · datahub_order_entries.Customer_Analytics_Measures</code>
- <code>Power BI · datahub_order_entries.Essential_KPI_Measures</code>
- <code>Power BI · datahub_order_entries.Geographic_Measures</code>
- <code>Power BI · datahub_order_entries.ORDER_DETAILS</code>
- <code>Power BI · datahub_order_entries.Product_Perfromance_Measures</code>
- <code>Power BI · datahub_order_entries.Time_Inteligence_Measures</code>
- <code>Snowflake · order_entry_db.analytics.order_details</code>
- <code>Snowflake · order_entry_db.analytics.order_details_replica</code>
- <code>dbt · ORDER_ENTRY_DB.analytics.order_details</code>

</details>

<details>
<summary>Cross-team owners (9)</summary>

- <code>group · 1e0398a3-113f-475e-b6fc-32ab72a634d2</code>
- <code>group · ORG_BACKEND_ENG</code>
- <code>user · alex@example.com</code>
- <code>user · brock1@example.com</code>
- <code>user · bryan@example.com</code>
- <code>user · jonny2@example.com</code>
- <code>user · kirk@example.com</code>
- <code>user · marty@example.com</code>
- <code>user · sam@example.com</code>

</details>

### 🚫 <code>critical_downstream</code> — BLOCK

**Why:** This change has critical or cross-team downstream consumers for dbt · order_entry_db.order_entry.customers.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers</code>](https://datahub.mlki.app/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- PII tags: <code>tag · PII_Data</code>
- Reaches: <code>Looker dashboard · dashboards.53</code>
- Blast radius: **16 downstream consumers** within 3 hops
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>dbt · ORDER_ENTRY_DB.analytics.order_details.cust_email</code> → <code>Snowflake · order_entry_db.analytics.order_details.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.224</code> → <code>Looker dashboard · dashboards.53</code>
- Path note: Column lineage is proven through the BI field; chart and dashboard hops are entity-level.

<details>
<summary>Downstream consumers (16)</summary>

- <code>Looker chart · dashboard_elements.221</code>
- <code>Looker chart · dashboard_elements.222</code>
- <code>Looker chart · dashboard_elements.223</code>
- <code>Looker chart · dashboard_elements.224</code>
- <code>Looker dashboard · dashboards.53</code>
- <code>Looker explore · order-entry.explore.order_details</code>
- <code>Looker view · order-entry-looker.view.order_details</code>
- <code>Power BI · datahub_order_entries.Customer_Analytics_Measures</code>
- <code>Power BI · datahub_order_entries.Essential_KPI_Measures</code>
- <code>Power BI · datahub_order_entries.Geographic_Measures</code>
- <code>Power BI · datahub_order_entries.ORDER_DETAILS</code>
- <code>Power BI · datahub_order_entries.Product_Perfromance_Measures</code>
- <code>Power BI · datahub_order_entries.Time_Inteligence_Measures</code>
- <code>Snowflake · order_entry_db.analytics.order_details</code>
- <code>Snowflake · order_entry_db.analytics.order_details_replica</code>
- <code>dbt · ORDER_ENTRY_DB.analytics.order_details</code>

</details>

<details>
<summary>Cross-team owners (9)</summary>

- <code>group · 1e0398a3-113f-475e-b6fc-32ab72a634d2</code>
- <code>group · ORG_BACKEND_ENG</code>
- <code>user · alex@example.com</code>
- <code>user · brock1@example.com</code>
- <code>user · bryan@example.com</code>
- <code>user · jonny2@example.com</code>
- <code>user · kirk@example.com</code>
- <code>user · marty@example.com</code>
- <code>user · sam@example.com</code>

</details>

---

Reproducibility: <code>policy_hash=baa612f729a56ff7497718cc3cf77cd9142967cb4ec0e075c2b3495eeb2f2927</code> · <code>commit_sha=d3f3bd2f4fe31837867592162ccea08859be6947</code> · run <code>sidq check --diff d3f3bd2f4fe31837867592162ccea08859be6947^..d3f3bd2f4fe31837867592162ccea08859be6947 --json</code>
