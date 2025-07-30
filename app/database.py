from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


import urllib.parse

SERVER_NAME_sql = 'DESKTOP-GNEUBQ4'
DATABASE_NAME_sql = 'quant_py_db'
USERNAME_sql = 'admin'
PASSWORD_sql = 'admin'

# The correct ODBC Driver name
ODBC_DRIVER = "{ODBC Driver 18 for SQL Server}" # Or "{ODBC Driver 17 for SQL Server}"

# Construct the ODBC connection string
odbc_conn_str = (
    f"DRIVER={ODBC_DRIVER};"
    f"SERVER={SERVER_NAME_sql};"
    f"DATABASE={DATABASE_NAME_sql};"
    f"UID={USERNAME_sql};"
    f"PWD={PASSWORD_sql};"
    f"Encrypt=no" # Add Encrypt=no if you need it, typically for older SQL Server versions or specific setups
)

# URL-encode the ODBC connection string
encoded_odbc_conn_str = urllib.parse.quote_plus(odbc_conn_str)

# This is your new, corrected SQLAlchemy URL
SQLALCHEMY_DATABASE_URL = f"mssql+pyodbc:///?odbc_connect={encoded_odbc_conn_str}"


engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Dependency for FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
