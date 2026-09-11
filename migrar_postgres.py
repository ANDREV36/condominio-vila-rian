import os
import sqlite3
import sys
from decimal import Decimal
from getpass import getpass

# Informe apenas no terminal a URL EXTERNA do PostgreSQL do Render.
# Ela não é gravada no arquivo nem no Git.
target_url = os.getenv('TARGET_DATABASE_URL') or getpass('Cole a External Database URL do PostgreSQL do Render (não será exibida): ')
source_path = sys.argv[1] if len(sys.argv) > 1 else 'condominio.db'

if not target_url.startswith(('postgres://', 'postgresql://')):
    raise SystemExit('URL PostgreSQL inválida.')

# Faz o app usar o PostgreSQL como destino e cria o schema isolado vila_rian.
os.environ['DATABASE_URL'] = target_url
os.environ.setdefault('SECRET_KEY', 'migration-only')

from app import app, db, ensure_postgres_schema, Configuracao, Usuario, Unidade, Pagamento, Despesa

if not os.path.exists(source_path):
    raise SystemExit(f'Banco SQLite não encontrado: {source_path}')

src = sqlite3.connect(source_path)
src.row_factory = sqlite3.Row

with app.app_context():
    ensure_postgres_schema()
    db.create_all()

    # O schema vila_rian é exclusivo deste condomínio; podemos substituir
    # uma migração anterior sem tocar nas tabelas dos outros projetos.
    with db.engine.begin() as conn:
        for table in (Pagamento.__table__, Despesa.__table__, Usuario.__table__, Unidade.__table__, Configuracao.__table__):
            conn.execute(table.delete())

    mappings = [
        (Configuracao, 'configuracao'),
        (Usuario, 'usuario'),
        (Unidade, 'unidade'),
        (Pagamento, 'pagamento'),
        (Despesa, 'despesa'),
    ]

    counts = {}
    with db.engine.begin() as conn:
        for model, table_name in mappings:
            cols = [c.name for c in model.__table__.columns]
            # Migra somente colunas presentes no SQLite e existentes no modelo.
            existing = {r[1] for r in src.execute(f'PRAGMA table_info({table_name})').fetchall()}
            cols = [c for c in cols if c in existing]
            if not cols:
                counts[table_name] = 0
                continue

            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table_name}").fetchall()
            inserted = 0
            for row in rows:
                values = {c: row[c] for c in cols}
                conn.execute(model.__table__.insert().values(**values))
                inserted += 1
            counts[table_name] = inserted

        # Recalibra sequences das PKs para que novos registros continuem normalmente.
        for table_name in ('configuracao', 'usuario', 'unidade', 'pagamento', 'despesa'):
            conn.exec_driver_sql(f"SELECT setval(pg_get_serial_sequence('vila_rian.{table_name}', 'id'), COALESCE((SELECT MAX(id) FROM vila_rian.{table_name}), 1), (SELECT MAX(id) IS NOT NULL FROM vila_rian.{table_name}))")

src.close()
print('\nMigração concluída com sucesso.')
for table, count in counts.items():
    print(f'  {table}: {count} registros')
print('Schema utilizado: vila_rian')
