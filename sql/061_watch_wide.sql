-- 061 (owner, 2026-10-20): "search for better pools, pools that pay; anything but memecoins".
--
-- Every Raydium and Orca concentrated pool with over $150k of daily volume whose
-- two tokens are majors, liquid staking tokens, BTC/ETH wrappers, stablecoins,
-- tokenized stocks (xStocks) or established project tokens (JUP, PYTH, RAY,
-- KMNO, MPLX, HYPE, ZEC, JLP, SPCX), less stable-to-stable pairs and pools
-- under $50k of liquidity (a few trades fake their volume). 46 pools, all read
-- by solana_state.fee_states with their active liquidity on 2026-10-20.
-- sol-usdc samples their fee counters every 10 minutes, read-only: they never
-- enter the ranking and no bot moves there. After 7 days each gets R (fee per
-- unit liquidity over sigma^2 sqrt(p)/4 per unit liquidity) and the fee
-- yield of a band, against 8sLb over the same hours.
--   cbBTC/USDC    0.04%  orca:HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM
--   SOL/ZEC       0.16%  orca:2TAgfogn8JRwwTnsGKUa6WUT5Xdv1iMEzfEDMor68X9C
--   ZEC/USDC      0.16%  orca:GTHKH8s82ZR8GTSFZ1dUu6wfdxhy59wpMShxzG5zjiPm
--   MPLX/SOL      0.16%  raydium-clmm:BxRhW4q1wTRwJkJ1C4NR4yJCRL2uusoju4g1bVenspZK
--   SOL/USDT      0.01%  raydium-clmm:3nMFwZXwY1s1M5s8vYAHqd4wGs4iSxXE4LRoUMMYqEgF
--   SOL/cbBTC     0.16%  orca:CeaZcxBNLpJWtxzt58qQmfMBtJY8pQLvursXTJYGQpbN
--   SOL/WBTC      0.05%  orca:B5EwJVDuAauzUEEdwvbuXzbFFgEYnUqqS37TUM1c4PQA
--   SPCX/USDC     0.25%  raydium-clmm:FjuBy7jjf9DXj9d3R7cHpvcnoFW2iQxf7F7P3vqx4Jza
--   SOL/RAY       0.05%  raydium-clmm:2AXXcN6oN9bBT5owwmTH53C7QHUXvhLeu718Kqt8rvY2
--   CRCLx/USDC    0.10%  raydium-clmm:GYqHjuDzTiw7i52Xv1qohDE6eJr6eSZpsrBVikGZyaFV
--   MPLX/USDC     0.05%  orca:78QBVQz3EnpuzmY42LRcCkn7gSyuFEMmQn1m5bY1yHgw
--   KMNO/USDC     0.01%  orca:3ndjN1nJVUKGrJBc1hhVpER6kWTZKHdyDrPyCJyX3CXK
--   SPYx/USDC     0.01%  raydium-clmm:4pCZCVEiYyT4efNdXUdL2tJF8VGMgiMXrZWq6FiNXhRw
--   NVDAx/USDC    0.10%  raydium-clmm:49iMatQtoyabsYAQc8GafVq6aeBFVDxSRH44oiatyyw6
--   TSLAx/USDC    0.10%  raydium-clmm:8aDaBQkTrS6HVMjyc6EZebgdiaXhLYGriDWKWWp1NpFF
--   JUP/SOL       0.05%  orca:C1MgLojNLWBKADvu9BHdtgzz1oZX4dZ5zGdGcgvvW8Wz
--   SOL/USDG      0.04%  orca:5KqohoeGjTjyHAFJJywK4J7fkFuK82PfMyuseGgLKZu2
--   SOL/MSTRx     0.25%  raydium-clmm:2ngTuP7xA581dqX9uJkGRqxmKuehY3k4SDfPebeoRG2J
--   SOL/JLP       0.04%  orca:6a3m2EgFFKfsFuQtP4LJJXPcAe3TQYXNyHUjjZpUxYgd
--   SPCX/SOL      0.04%  orca:58iQRQbAKgPU2ADzRLS4QJxBS2RsdUTEd567dPpMcLHe
--   SOL/HYPE      0.30%  orca:31KrYUDzgEQhEgr1JSNVfHAknWACcF97CtUaU8enKQsy
--   SOL/USDC      0.01%  orca:83v8iPyZihDEjDdY8RdZddyZNyUtXngz69Lgo9Kt5d6d
--   SOL/USD1      0.25%  raydium-clmm:AQAGYQsdU853WAKhXM79CgNdoyhrRwXvYHX6qrDyC1FS
--   NVDAx/USDC    0.02%  orca:6R4r93V5fcMzc13CL2enEepDSYcr4Qx3ptZBDwudTXCo
--   SOL/USDC      0.02%  raydium-clmm:CYbD9RaToYMtWKA7QZyoLahnHdWq553Vm62Lh6qWtuxq
--   SPCXx/USDC    0.05%  orca:ASAxmEaTT1HFe3mVC3zbKDE4tuB28W7732XQrEMBM5W2
--   MSTRx/USDC    0.25%  raydium-clmm:RyhF4cksVZY7vcqJpoytHcxcGNKRp27PEGhSnEPpbGv
--   MSFTx/USDC    0.25%  raydium-clmm:CLu4kFM4nb67xrdN7vJnMxXXir8Z5hA4HJUzPFccXjsL
--   SOL/NVDAx     0.25%  raydium-clmm:38rXq2BuwdNcfnwydw6S2srpNK9N4gVzf39f69suPMsF
--   SPCX/SOL      0.60%  raydium-clmm:7tJVK2uEaK1tKEykDHQ8KHdaV3aXrVuSH1J4u3xgkS7U
--   cbBTC/ZEC     0.04%  orca:3BRFxaZBNVwz5FmYCLWjEcozJws1enVzsJvtaCY1oQH5
--   SOL/SPYx      0.25%  raydium-clmm:BS9uyGV6XmNnPkM4f3xgxCdQEaFv7RSKs6fwrpvYHxfL
--   METAx/USDC    0.25%  raydium-clmm:3L7KbPVaAQA4UTecaGQYsm6UCq5F3sZM9zAYkxqYt63j
--   GOOGLx/USDC   0.25%  raydium-clmm:B8YAwjGYk6qidWzGBXMAxP7nYfG8g74EZ3Y4gFSsobRw
--   SOL/NVDAx     0.16%  orca:8Vy5Rjb9vg5nLb8L1r4Di8p9py5BTkp8UJSMHG8AD2zs
--   JLP/USDC      0.05%  orca:HD8i7qr1hd9ida6sN71RbkLxbWcbvZS4NA5CY6vfcDpj
--   QQQx/USDC     0.10%  raydium-clmm:GMjGLWzvK75LPetrgAmdeXnvxc4fUuQPwJxeQqTDU1aG
--   TSLAx/USDC    0.02%  orca:9p7abUFv31ycgu9kckvnoqMMvBy67dqTDM2m6HP9xokN
--   GLDx/USDC     0.04%  orca:5tGLudhm9pHtqbtfymvdarbD59AyMyYimXXwbFfzETV9
--   SOL/PYTH      0.25%  raydium-clmm:9n3dSLrERZQp95dHXywft7xV8D8xnGFLaUHtEhQVaXaC
--   COINx/USDC    0.80%  raydium-clmm:w7SGmPeXoMCsjvXqgsAmUn56uypyDsjAtsxeVkaiqxa
--   SOL/PYTH      0.05%  orca:8erNF5u3CHrqZJXtkfY8CjSxFYF1yqHmN8uDbAhk6tWM
--   SOL/SPCXx     0.80%  raydium-clmm:DNcGcbmmmvjrmVsT4DTMh2FLrbBchxXd66njTKo6zDAc
--   USDC/JitoSOL  0.05%  orca:5hWJUNTtEtKmKgDXpthJXXRRmJrz5vJ7uJzrUNVdrwLg
--   SOL/USDC      0.05%  raydium-clmm:2QdhepnKRTLjjSqPL1PtKNwqrUkoLee5Gqs8bvZhRdMv
--   SOL/USDT      0.02%  orca:FwewVm8u6tFPGewAyHmWAqad9hmF7mvqxK4mJ7iNqqGC
begin;
set local lock_timeout = '5s';
update rebalancer.config
   set venue_watch = array['orca:HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM',
                           'orca:2TAgfogn8JRwwTnsGKUa6WUT5Xdv1iMEzfEDMor68X9C',
                           'orca:GTHKH8s82ZR8GTSFZ1dUu6wfdxhy59wpMShxzG5zjiPm',
                           'raydium-clmm:BxRhW4q1wTRwJkJ1C4NR4yJCRL2uusoju4g1bVenspZK',
                           'raydium-clmm:3nMFwZXwY1s1M5s8vYAHqd4wGs4iSxXE4LRoUMMYqEgF',
                           'orca:CeaZcxBNLpJWtxzt58qQmfMBtJY8pQLvursXTJYGQpbN',
                           'orca:B5EwJVDuAauzUEEdwvbuXzbFFgEYnUqqS37TUM1c4PQA',
                           'raydium-clmm:FjuBy7jjf9DXj9d3R7cHpvcnoFW2iQxf7F7P3vqx4Jza',
                           'raydium-clmm:2AXXcN6oN9bBT5owwmTH53C7QHUXvhLeu718Kqt8rvY2',
                           'raydium-clmm:GYqHjuDzTiw7i52Xv1qohDE6eJr6eSZpsrBVikGZyaFV',
                           'orca:78QBVQz3EnpuzmY42LRcCkn7gSyuFEMmQn1m5bY1yHgw',
                           'orca:3ndjN1nJVUKGrJBc1hhVpER6kWTZKHdyDrPyCJyX3CXK',
                           'raydium-clmm:4pCZCVEiYyT4efNdXUdL2tJF8VGMgiMXrZWq6FiNXhRw',
                           'raydium-clmm:49iMatQtoyabsYAQc8GafVq6aeBFVDxSRH44oiatyyw6',
                           'raydium-clmm:8aDaBQkTrS6HVMjyc6EZebgdiaXhLYGriDWKWWp1NpFF',
                           'orca:C1MgLojNLWBKADvu9BHdtgzz1oZX4dZ5zGdGcgvvW8Wz',
                           'orca:5KqohoeGjTjyHAFJJywK4J7fkFuK82PfMyuseGgLKZu2',
                           'raydium-clmm:2ngTuP7xA581dqX9uJkGRqxmKuehY3k4SDfPebeoRG2J',
                           'orca:6a3m2EgFFKfsFuQtP4LJJXPcAe3TQYXNyHUjjZpUxYgd',
                           'orca:58iQRQbAKgPU2ADzRLS4QJxBS2RsdUTEd567dPpMcLHe',
                           'orca:31KrYUDzgEQhEgr1JSNVfHAknWACcF97CtUaU8enKQsy',
                           'orca:83v8iPyZihDEjDdY8RdZddyZNyUtXngz69Lgo9Kt5d6d',
                           'raydium-clmm:AQAGYQsdU853WAKhXM79CgNdoyhrRwXvYHX6qrDyC1FS',
                           'orca:6R4r93V5fcMzc13CL2enEepDSYcr4Qx3ptZBDwudTXCo',
                           'raydium-clmm:CYbD9RaToYMtWKA7QZyoLahnHdWq553Vm62Lh6qWtuxq',
                           'orca:ASAxmEaTT1HFe3mVC3zbKDE4tuB28W7732XQrEMBM5W2',
                           'raydium-clmm:RyhF4cksVZY7vcqJpoytHcxcGNKRp27PEGhSnEPpbGv',
                           'raydium-clmm:CLu4kFM4nb67xrdN7vJnMxXXir8Z5hA4HJUzPFccXjsL',
                           'raydium-clmm:38rXq2BuwdNcfnwydw6S2srpNK9N4gVzf39f69suPMsF',
                           'raydium-clmm:7tJVK2uEaK1tKEykDHQ8KHdaV3aXrVuSH1J4u3xgkS7U',
                           'orca:3BRFxaZBNVwz5FmYCLWjEcozJws1enVzsJvtaCY1oQH5',
                           'raydium-clmm:BS9uyGV6XmNnPkM4f3xgxCdQEaFv7RSKs6fwrpvYHxfL',
                           'raydium-clmm:3L7KbPVaAQA4UTecaGQYsm6UCq5F3sZM9zAYkxqYt63j',
                           'raydium-clmm:B8YAwjGYk6qidWzGBXMAxP7nYfG8g74EZ3Y4gFSsobRw',
                           'orca:8Vy5Rjb9vg5nLb8L1r4Di8p9py5BTkp8UJSMHG8AD2zs',
                           'orca:HD8i7qr1hd9ida6sN71RbkLxbWcbvZS4NA5CY6vfcDpj',
                           'raydium-clmm:GMjGLWzvK75LPetrgAmdeXnvxc4fUuQPwJxeQqTDU1aG',
                           'orca:9p7abUFv31ycgu9kckvnoqMMvBy67dqTDM2m6HP9xokN',
                           'orca:5tGLudhm9pHtqbtfymvdarbD59AyMyYimXXwbFfzETV9',
                           'raydium-clmm:9n3dSLrERZQp95dHXywft7xV8D8xnGFLaUHtEhQVaXaC',
                           'raydium-clmm:w7SGmPeXoMCsjvXqgsAmUn56uypyDsjAtsxeVkaiqxa',
                           'orca:8erNF5u3CHrqZJXtkfY8CjSxFYF1yqHmN8uDbAhk6tWM',
                           'raydium-clmm:DNcGcbmmmvjrmVsT4DTMh2FLrbBchxXd66njTKo6zDAc',
                           'orca:5hWJUNTtEtKmKgDXpthJXXRRmJrz5vJ7uJzrUNVdrwLg',
                           'raydium-clmm:2QdhepnKRTLjjSqPL1PtKNwqrUkoLee5Gqs8bvZhRdMv',
                           'orca:FwewVm8u6tFPGewAyHmWAqad9hmF7mvqxK4mJ7iNqqGC'],
       updated_at = now()
 where name = 'sol-usdc';
commit;
