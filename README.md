# Frontend Quant

Frontend Quant is developed using:

- **HTML** & **CSS** for the user interface
- **Alpine.js** for lightweight interactivity
- **FastAPI** for the backend API
- **SQLAlchemy** for database management

---

## Workflow

- The **index.html** and **backtest.html** pages allow users to select backtest settings and generate indicator values using custom logic.
- When running a backtest, the frontend calls a REST API provided by a Spring Boot project to execute the backtest and retrieve results.

---

## Architecture

<details> <summary>Click to expand</summary>
+---------------------+      +-----------------------+      +------------------------+      +---------------------+
|      User           |----->|    React Frontend     |<---->|     FastAPI Backend    |<---->|       MsSql         |
| (Web Browser)       |      |   (View Layer)        |      | (API & Business Logic) |      | (Data Persistence)  |
+---------------------+      +-----------------------+      +------------------------+      +---------------------+
                                |        ^      ^                                               |    |    |    |
                                |        |      |                                               |    |    |    V
                                |        |      +------------Request Data-----------------------+    |    |  [Tables]
                                |        |                                                            |    |  - Strategies
                                |        +----------------Response Data------------------------+    |  - EquityHistory
                                |                                                                    |  - TradeList
                                +------------User Actions (View, Upload)------------------------>    |    (entry_date,
                                                                                                     |     exit_date,
                                                                                                     |     trade_type,
                                                                                                     |     status)
                                                                                                     |  - PortfolioMetrics

</details>