Offline experiments for a fee-income LP strategy with a 50/50 payout/reinvestment rule.

Read [REPORT.md](REPORT.md) for the results and limitations.
Read [PROTOCOL.md](PROTOCOL.md) for the pre-result design.

The data archive contains public Orca SOL/USDC five-minute OHLCV and one current Orca pool snapshot.
The scripts do not read wallet credentials, access the bot database, or send transactions.

From the repository root:

~~~sh
python3 -m unittest discover -s research/income_experiment -p test_research.py -v
python3 research/income_experiment/run_experiments.py select
python3 research/income_experiment/run_experiments.py evaluate
python3 research/income_experiment/diagnostics.py
python3 research/income_experiment/render_figures.py
~~~

The selection stage reads training and validation observations.
The evaluation stage checks the frozen dataset and implementation hashes.
Running selection again replaces the saved selection with the same deterministic calculation.

Core dependencies: Python and NumPy. Figures additionally require Matplotlib.
During this session, plotting dependencies were installed in /tmp/lp-research-plot.
The renderer uses that directory only if Matplotlib is unavailable in the active environment.

The fetch script preserves an existing assembled archive.
The data manifest records response URLs and SHA-256 hashes.
Do not replace the dataset and present the resulting evaluation as the original frozen experiment.

The primary suite contains 26 policies, two initial balances, and a 60/20/20 chronological split.
The capital-transfer diagnostics are post-result exploratory checks.
The figures are available as PNG, SVG, and PDF in results/.

The cash-flow figures are conditional scenarios.
Historical active liquidity, fee-token composition, actual execution routes, and transaction failures are not reconstructed.
