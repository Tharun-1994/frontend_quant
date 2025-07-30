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

flowchart LR
    A([🧑‍💻 User<br/>(Web Browser)])
    B([⚛️ React Frontend<br/>(View Layer)])
    C([🚀 FastAPI Backend<br/>(API & Business Logic)])
    D([🗄️ MSSQL Database])

    A -->|User Interaction| B
    B -->|Fetch/Post Requests| C
    C -->|SQL Queries| D
    D -->|Query Results| C
    C -->|JSON Response| B

    subgraph 📂 Tables
        T1[📋 Strategies]
        T2[📈 EquityHistory]
        T3[📄 TradeList<br/>(entry_date, exit_date, trade_type, status)]
        T4[📊 PortfolioMetrics]
    end

    D --> T1
    D --> T2
    D --> T3
    D --> T4
