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
    A([🧑 User<br/>(Web Browser)])
    B([⚛️ React Frontend<br/>(View Layer)])
    C([🚀 FastAPI Backend<br/>(API & Logic)])
    D([🗄️ MsSql<br/>(Database)])

    A -- User Actions --> B
    B -- Request Data --> C
    C -- DB Queries --> D
    D -- Data --> C
    C -- Response Data --> B

    subgraph Tables [Tables]
        T1[Strategies]
        T2[EquityHistory]
        T3[TradeList<br/>(entry_date, exit_date, trade_type, status)]
        T4[PortfolioMetrics]
    end

    D --> T1
    D --> T2
    D --> T3
    D --> T4
```