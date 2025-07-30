# 📘 TradeLogic Backtest Platform

An end-to-end strategy research and backtesting platform built using **React**, **FastAPI**, and **MsSQL**. Designed to manage strategies, run customizable backtests, and track performance via equity curves, tradelists, and metrics.

---

## 🚀 Features

* Strategy builder UI (Alpine.js + Tailwind)
* Entry/Exit condition editor with dynamic rules
* FastAPI backend with SQLAlchemy + Alembic
* Persistent database (MsSQL or PostgreSQL)
* Auto-generated equity/tradelist tables
* Modular architecture (Frontend, Backend, DB)
* Strategy saving and validation
* Rule expression building

---

## 🧩 Architecture

<details>
<summary>Click to expand</summary>

```text
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
```

</details>

---

## 🛠️ Setup Instructions

### 📦 Backend (FastAPI + Alembic)

```bash
cd backend
pip install -r requirements.txt
alembic upgrade head
uvicorn main:app --reload
```

### 🌐 Frontend (HTML + Tailwind + Alpine.js)

* Static HTML served from templates
* Use `x-model` and fetch API for POST calls

### ⚙️ Configuration

* Database URL (in `.env`):

```
SQLALCHEMY_DATABASE_URL=mssql+pyodbc://admin:admin@HOST/DATABASE?driver=ODBC+Driver+17+for+SQL+Server
```

---

## 🧠 Strategy Schema

```python
class Strategy(Base):
    id = Column(Integer, primary_key=True)
    name = Column(String)
    universe = Column(String)
    slots = Column(Integer)
    capital = Column(Decimal)
    start_date = Column(Date)
    end_date = Column(Date)
    stoploss_pct = Column(Decimal)
    takeprofit_pct = Column(Decimal)
    entry_rules = Column(Text)  # Stored as string: "rsi_2 < 20 & hv_100 > 52"
    exit_rules = Column(Text)
    ranking = Column(String)
```

### ✏️ Rule Expression

```text
RSI + Lookback 2 + < + Value 20 --> becomes rsi_2 < 20
Multiple rules joined by connectors: AND (&&), OR (||)
```

---

## 💡 Design Notes

* Universes, indicators, operators are injected as static dicts
* Entry/Exit rules are modeled with Pydantic and parsed server-side
* Alembic manages table migrations
* Errors are handled using FastAPI exception middleware

---

## 📎 Future Enhancements

* Chart preview for backtests
* Drag-drop condition builder
* Plotly/Charts integration
* Indicator calculations based on scheduler-loaded close prices

---

## 📂 Project Structure

```text
app/
├── database.py
├── main.py
├── models/
│   └── strategy.py
├── routes/
│   └── strategy.py
├── schemas/
│   └── backtest_request.py
├── constants/
│   └── static_config.py
alembic/
├── versions/
│   └── create_strategies_table.py
templates/
└── index.html
```

---

## 📬 Contact

For enhancements or issues, open a GitHub issue or reach out at: `your_email@domain.com`

---

> Built with ❤️ for quants and strategy researchers.
