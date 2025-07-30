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

```mermaid
flowchart LR
    User([User<br/>(Web Browser)])
    Frontend([React Frontend<br/>(View Layer)])
    Backend([FastAPI Backend<br/>(API & Business Logic)])
    DB([MsSql<br/>(Data Persistence)])

    User -->|User Actions| Frontend
    Frontend -->|Request Data| Backend
    Backend -->|DB Queries| DB
    DB -->|Data| Backend
    Backend -->|Response Data| Frontend

    subgraph Tables [ ]
        Strategies[Strategies]
        EquityHistory[EquityHistory]
        TradeList[TradeList<br/>(entry_date, exit_date, trade_type, status)]
        PortfolioMetrics[PortfolioMetrics]
    end

    DB --> Strategies
    DB --> EquityHistory
    DB --> TradeList
    DB --> PortfolioMetrics
```