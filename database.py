import sqlite3

def init_db(db_path="consultas.db"):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()


    c.execute("""
        CREATE TABLE IF NOT EXISTS consultas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cpf TEXT,
            data TEXT,
            resultado TEXT
        )
    """)

    # garante que a coluna lote_id existe
    c.execute("PRAGMA table_info(consultas)")
    colunas = [row[1] for row in c.fetchall()]
    if "lote_id" not in colunas:
        c.execute("ALTER TABLE consultas ADD COLUMN lote_id TEXT")

    # índices
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_consultas_cpf_lote ON consultas(cpf, lote_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_consultas_lote_id ON consultas(lote_id)")

    conn.commit()
    conn.close()
