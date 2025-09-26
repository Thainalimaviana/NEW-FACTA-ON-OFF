from flask import Flask, request, jsonify, render_template, send_file
import requests
import pandas as pd
import io
import sqlite3
from datetime import datetime, timedelta
import os
import unicodedata
from database import init_db
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

app = Flask(__name__)
init_db()

DB_FILE = "consultas.db"
RESULT_FOLDER = "resultados"
os.makedirs(RESULT_FOLDER, exist_ok=True)
os.makedirs("recuperacoes", exist_ok=True)

token_offline = None
requisicoes_usadas_com_token = 0
LIMITE_REQS_POR_TOKEN = 5000
TOKEN_URL_OFF = "https://fgtsoff.facta.com.br/gera-token"
TOKEN_AUTH_HEADER_OFF = "Basic OTY1NTI6ZjRzaXV0azJ1ZWNhNDVldXhnOXc="
API_URL_OFF = "https://fgtsoff.facta.com.br/fgts/base-offline"

token_online = None
token_expira_em = None
TOKEN_URL_ON = "https://webservice.facta.com.br/gera-token"
TOKEN_AUTH_HEADER_ON = "Basic OTY1NTI6ZjRzaXV0azJ1ZWNhNDVldXhnOXc="
API_URL_ON = "https://webservice.facta.com.br/fgts/saldo"

executor = ThreadPoolExecutor(max_workers=3)

@app.route("/")
def menu():
    return render_template("index.html")

@app.route("/offline")
def tela_offline():
    return render_template("index_offline.html")

@app.route("/online")
def tela_online():
    return render_template("index_online.html")


def gerar_token_offline():
    response = requests.get(
        TOKEN_URL_OFF,
        headers={
            "Authorization": TOKEN_AUTH_HEADER_OFF,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"
        },
        timeout=10
    )
    data = {}
    try:
        data = response.json()
    except Exception:
        print("⚠️ Erro ao converter token OFFLINE para JSON")
    return data.get("token")

def gerar_token_online():
    global token_online, token_expira_em
    resp = requests.get(
        TOKEN_URL_ON,
        headers={"Authorization": TOKEN_AUTH_HEADER_ON, "Accept": "application/json"},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    token_online = data.get("token")
    token_expira_em = datetime.now() + timedelta(minutes=59)
    return token_online

def garantir_token_online():
    global token_online, token_expira_em
    if token_online is None or token_expira_em is None:
        return gerar_token_online()
    if datetime.now() >= token_expira_em:
        return gerar_token_online()
    return token_online

def normalizar(txt):
    return unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode().lower()


def consulta_cpf_offline(cpf, max_tentativas, lote_id):
    global token_offline, requisicoes_usadas_com_token
    tentativa = 0
    resultado_final = {"CPF": cpf, "Resultado": "Pendente"}

    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO consultas (cpf, resultado, data, lote_id) VALUES (?, ?, ?, ?)",
              (cpf, "Pendente", ts_now, lote_id))
    conn.commit()
    conn.close()

    while tentativa < max_tentativas:
        tentativa += 1
        try:
            if token_offline is None or requisicoes_usadas_com_token >= LIMITE_REQS_POR_TOKEN:
                token_offline = gerar_token_offline()
                requisicoes_usadas_com_token = 0
                print(f"\n✅ Novo token OFFLINE gerado: {token_offline[:20]}...\n")

            print(f"\n➡️ CONSULTA OFFLINE CPF {cpf} | Tentativa {tentativa}/{max_tentativas}")

            response = requests.get(
                API_URL_OFF,
                headers={
                    "Authorization": f"Bearer {token_offline}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0"
                },
                params={"cpf": cpf},
                timeout=15,
            )
            requisicoes_usadas_com_token += 1

            print(f"📡 STATUS: {response.status_code}")
            print(f"📦 BODY: {response.text}\n")

            resp_json = {}
            try:
                resp_json = response.json()
            except Exception:
                print("⚠️ Resposta não era JSON válido")

            mensagem = (resp_json.get("mensagem", "") or "")
            erro_flag = resp_json.get("erro", True)
            msg_norm = normalizar(mensagem)

            if "base offline indisponivel" in msg_norm:
                print("⚠️ Base offline indisponível, tentando novamente...")
                if tentativa < max_tentativas:
                    time.sleep(2)
                    continue
                else:
                    resultado_final["Resultado"] = "Limite de tentativas atingido"
                    break

            if not erro_flag:
                resultado_final["Resultado"] = mensagem or "Autorizado"
                break

            resultado_final["Resultado"] = mensagem or "Não autorizado"
            break

        except Exception as e:
            print(f"❌ Erro na tentativa {tentativa}/{max_tentativas} CPF {cpf}: {e}")
            if tentativa >= max_tentativas:
                resultado_final["Resultado"] = "Erro"

        finally:
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE consultas SET resultado=?, data=? WHERE cpf=? AND lote_id=?",
                      (resultado_final["Resultado"], ts_now, cpf, lote_id))
            conn.commit()
            conn.close()

            print(f"💾 Resultado salvo no banco | CPF {cpf} -> {resultado_final['Resultado']}")

    return resultado_final

@app.route("/consultar-offline", methods=["POST"])
def consultar_offline():
    data_in = request.get_json(silent=True) or {}
    cpfs = data_in.get("cpfs", [])
    max_tentativas = int(data_in.get("tentativas", 1))
    lote_id = data_in.get("lote_id")

    if not cpfs:
        return jsonify({"erro": "Lista de CPFs vazia."}), 400

    futures = [executor.submit(consulta_cpf_offline, cpf, max_tentativas, lote_id) for cpf in cpfs]
    resultados = [f.result() for f in as_completed(futures)]
    return jsonify(resultados)


def consulta_cpf_online(cpf, lote_id):
    resultado_final = {"CPF": cpf, "Resultado": "Pendente"}

    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO consultas (cpf, resultado, data, lote_id) VALUES (?, ?, ?, ?)",
              (cpf, "Pendente", ts_now, lote_id))
    conn.commit()
    conn.close()

    try:
        token = garantir_token_online()
        response = requests.get(
            API_URL_ON,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"cpf": cpf},
            timeout=10,
        )

        if response.status_code == 200:
            resp_json = response.json()
            erro_flag = resp_json.get("erro", False)
            mensagem = resp_json.get("mensagem", "")

            if not erro_flag:
                if "retorno" in resp_json:
                    dados = resp_json.get("retorno", {})
                    saldo_bruto = dados.get("saldo_total", "0")
                    resultado_final["Resultado"] = f"Saldo Bruto: {saldo_bruto}"
                else:
                    resultado_final["Resultado"] = mensagem or "Autorizado"
            else:
                resultado_final["Resultado"] = mensagem or "Não autorizado"
        else:
            resultado_final["Resultado"] = f"Erro HTTP {response.status_code}"

    except Exception as e:
        resultado_final["Resultado"] = f"Erro: {str(e)}"


    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE consultas SET resultado=?, data=? WHERE cpf=? AND lote_id=?",
              (resultado_final["Resultado"], ts_now, cpf, lote_id))
    conn.commit()
    conn.close()

    return resultado_final

@app.route("/consultar-online", methods=["POST"])
def consultar_online():
    data_in = request.get_json(silent=True) or {}
    cpfs = data_in.get("cpfs", [])
    lote_id = data_in.get("lote_id")

    if not cpfs:
        return jsonify({"erro": "Lista de CPFs vazia."}), 400

    futures = [executor.submit(consulta_cpf_online, cpf, lote_id) for cpf in cpfs]
    resultados = [f.result() for f in as_completed(futures)]
    return jsonify(resultados)


@app.route("/baixar-excel/<lote_id>")
def baixar_excel(lote_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT cpf, resultado, data FROM consultas WHERE lote_id=? ORDER BY id", (lote_id,))
    rows = c.fetchall()
    conn.close()

    resultados = [{"CPF": r[0], "Resultado": r[1], "Data": r[2]} for r in rows]

    if not resultados:
        return jsonify({"erro": f"Nenhum resultado encontrado para o lote {lote_id}"}), 400

    df = pd.DataFrame(resultados)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=f"Lote_{lote_id}")
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=f"resultado_{lote_id}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.route("/status-lote/<lote_id>")
def status_lote(lote_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT cpf, resultado, data FROM consultas WHERE lote_id=? ORDER BY id", (lote_id,))
    rows = c.fetchall()
    conn.close()

    return jsonify([{"CPF": r[0], "Resultado": r[1], "Data": r[2]} for r in rows])

@app.route("/recuperar-ultimos")
def recuperar_ultimos():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT lote_id FROM consultas WHERE lote_id IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify([])

    ultimo_lote = row[0]
    c.execute("SELECT cpf, resultado, data FROM consultas WHERE lote_id=? ORDER BY id", (ultimo_lote,))
    rows = c.fetchall()
    conn.close()

    return jsonify([{"CPF": r[0], "Resultado": r[1], "Data": r[2]} for r in rows])


if __name__ == "__main__":
    app.run(debug=True, port=8800)
