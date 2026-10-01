{#- The collector's raw files: one gzip JSONL file per collection hour, possibly several gzip
    members, possibly re-saved trades after a restart. Columns are typed explicitly so the
    trade_id hash matches the app's loader byte for byte. `newer_than` (a 'YYYY/MM/DD/HH' path
    suffix) skips older files on incremental runs. -#}
{% macro raw_trades(newer_than=none) %}
(
  select *
  from read_json(
    '{{ env_var("MARKETCASE_RAW") }}/trades/*/*/*/*.jsonl.gz',
    format = 'newline_delimited',
    compression = 'gzip',
    filename = true,
    columns = {proxyWallet: 'VARCHAR', side: 'VARCHAR', asset: 'VARCHAR', conditionId: 'VARCHAR',
               size: 'DOUBLE', price: 'DOUBLE', timestamp: 'BIGINT', title: 'VARCHAR',
               slug: 'VARCHAR', eventSlug: 'VARCHAR', outcome: 'VARCHAR',
               transactionHash: 'VARCHAR', collected_at: 'VARCHAR'}
  )
  {% if newer_than %}
  where filename >= '{{ env_var("MARKETCASE_RAW") }}/trades/{{ newer_than }}'
  {% endif %}
)
{% endmacro %}

{% macro market_exports() %}
read_parquet('{{ env_var("MARKETCASE_EXPORTS") }}/markets.parquet')
{% endmacro %}
