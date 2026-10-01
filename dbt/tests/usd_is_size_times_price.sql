-- Every fill's dollar value is its size times its price.
select trade_id from {{ ref('fct_trades') }} where abs(usd - size * price) > 1e-6
