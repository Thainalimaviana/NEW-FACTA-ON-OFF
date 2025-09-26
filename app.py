from flask import Flask, request, jsonify, render_template, send_file
import requests
import pandas as pd
import io
import re
import time
import sqlite3
from datetime import datetime, timedelta
import os
import unicodedata
from database import init_db

app = Flask(__name__)

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
            "User-Agent": "insomnia/11.2.0"
        },
        timeout=10
    )

    print("\n=== GERAR TOKEN OFFLINE ===")
    print(f"STATUS: {response.status_code}")
    print(f"BODY: {response.text}\n")

    data = response.json()
    return data.get("token")

def normalizar(txt):
    return unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode().lower()

def gerar_token_online():
    global token_online, token_expira_em
    resp = requests.get(
        TOKEN_URL_ON,
        headers={"Authorization": TOKEN_AUTH_HEADER_ON, "Accept": "application/json"},
        timeout=10
    )
    print("\n=== GERAR TOKEN ONLINE ===")
    print(f"STATUS: {resp.status_code}")
    print(f"BODY: {resp.text}\n")

    resp.raise_for_status()
    data = resp.json()
    novo_token = data.get("token")
    if not novo_token:
        raise RuntimeError(f"Não recebi token. Resposta: {resp.text}")

    token_online = novo_token
    token_expira_em = datetime.now() + timedelta(minutes=59)
    print(f"Novo token (ONLINE): {token_online[:15]}... válido até {token_expira_em.strftime('%H:%M:%S')}")
    return token_online

def garantir_token_online():
    global token_online, token_expira_em
    if token_online is None or token_expira_em is None:
        return gerar_token_online()
    if datetime.now() >= token_expira_em:
        return gerar_token_online()
    return token_online

@app.route("/consultar-offline", methods=["POST"])
def consultar_offline():
    global token_offline, requisicoes_usadas_com_token
    data_in = request.get_json(silent=True) or {}
    cpfs = data_in.get("cpfs", [])
    max_tentativas = int(data_in.get("tentativas", 1))
    lote_id = data_in.get("lote_id")

    if not isinstance(cpfs, list) or not cpfs:
        return jsonify({"erro": "Lista de CPFs vazia."}), 400

    def garantir_token():
        global token_offline, requisicoes_usadas_com_token
        if token_offline is None or requisicoes_usadas_com_token >= LIMITE_REQS_POR_TOKEN:
            novo = gerar_token_offline()
            if not novo:
                raise RuntimeError("Não foi possível gerar o token OFFLINE")
            token_offline = novo
            requisicoes_usadas_com_token = 0
            print("Novo token OFFLINE gerado.")

    resultados = []
    for cpf in cpfs:
        tentativa = 0
        resultado_final = {"CPF": cpf, "Resultado": "Erro"}
        while tentativa < max_tentativas:
            tentativa += 1
            try:
                garantir_token()
                response = requests.get(
                    API_URL_OFF,
                    headers={
                        "Authorization": f"Bearer {token_offline}",
                        "Accept": "application/json",
                        "User-Agent": "insomnia/11.2.0"
                    },
                    params={"cpf": cpf},
                    timeout=15,
                )

                print(f"\n➡️ CONSULTA OFFLINE CPF {cpf} | Tentativa {tentativa}/{max_tentativas}")
                print(f"STATUS: {response.status_code}")
                print(f"BODY: {response.text}\n")

                requisicoes_usadas_com_token += 1
                resp_json = response.json()
                mensagem = (resp_json.get("mensagem", "") or "")
                erro_flag = resp_json.get("erro", True)
                msg_norm = normalizar(mensagem)

                if "base offline indisponivel" in msg_norm:
                    if tentativa < max_tentativas:
                        print(f"⚠️ Base indisponível. Reconsultando CPF {cpf} em 2s...")
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
                print(f"❌ Erro na tentativa {tentativa}/{max_tentativas} para CPF {cpf}: {e}")
                if tentativa < max_tentativas:
                    time.sleep(5)
                    continue
                else:
                    resultado_final["Resultado"] = "Limite de tentativas atingido"
                    break

        resultados.append(resultado_final)

        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if lote_id:
            c.execute("UPDATE consultas SET resultado=?, data=? WHERE cpf=? AND lote_id=?",
                      (resultado_final["Resultado"], ts_now, resultado_final["CPF"], lote_id))
            if c.rowcount == 0:
                c.execute("INSERT INTO consultas (cpf, resultado, data, lote_id) VALUES (?, ?, ?, ?)",
                          (resultado_final["CPF"], resultado_final["Resultado"], ts_now, lote_id))
        else:
            c.execute("INSERT INTO consultas (cpf, resultado, data) VALUES (?, ?, ?)",
                      (resultado_final["CPF"], resultado_final["Resultado"], ts_now))
        conn.commit()
        conn.close()

    return jsonify(resultados)

@app.route("/consultar-online", methods=["POST"])
def consultar_online():
    data_in = request.get_json(silent=True) or {}
    cpfs = data_in.get("cpfs", [])
    lote_id = data_in.get("lote_id")

    if not isinstance(cpfs, list) or not cpfs:
        return jsonify({"erro": "Lista de CPFs vazia."}), 400

    resultados = []
    for cpf in cpfs:
        resultado_final = {"CPF": cpf, "Resultado": "Erro", "Status": "Não autorizado"}
        try:
            token = garantir_token_online()
            response = requests.get(
                API_URL_ON,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"cpf": cpf},
                timeout=15,
            )

            if response.status_code == 200:
                resp_json = response.json()

                if "token inválido" in normalizar(resp_json.get("mensagem", "")):
                    token = gerar_token_online()
                    response = requests.get(
                        API_URL_ON,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        params={"cpf": cpf},
                        timeout=15,
                    )
                    resp_json = response.json()
            else:
                resp_json = {}

            if response.status_code == 200:
                erro_flag = resp_json.get("erro", False)
                mensagem = resp_json.get("mensagem", "")

                if not erro_flag:
                    if "retorno" in resp_json:
                        dados = resp_json.get("retorno", {})
                        saldo_bruto = dados.get("saldo_total", "0")
                        saldo_liquido = saldo_bruto
                        resultado_final["Resultado"] = f"Saldo Bruto: {saldo_bruto} | Saldo Líquido: {saldo_liquido}"
                        resultado_final["Status"] = "Autorizado"
                    else:
                        resultado_final["Resultado"] = mensagem or "Autorizado"
                        resultado_final["Status"] = "Autorizado"
                else:
                    resultado_final["Resultado"] = mensagem or "Não autorizado"
                    resultado_final["Status"] = "Não autorizado"
            else:
                resultado_final["Resultado"] = f"Erro HTTP {response.status_code}"

        except Exception as e:
            resultado_final["Resultado"] = f"Erro: {str(e)}"

        resultados.append(resultado_final)

        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if lote_id:
            c.execute("UPDATE consultas SET resultado=?, data=? WHERE cpf=? AND lote_id=?",
                      (resultado_final["Resultado"], ts_now, resultado_final["CPF"], lote_id))
            if c.rowcount == 0:
                c.execute("INSERT INTO consultas (cpf, resultado, data, lote_id) VALUES (?, ?, ?, ?)",
                          (resultado_final["CPF"], resultado_final["Resultado"], ts_now, lote_id))
        else:
            c.execute("INSERT INTO consultas (cpf, resultado, data) VALUES (?, ?, ?)",
                      (resultado_final["CPF"], resultado_final["Resultado"], ts_now))
        conn.commit()
        conn.close()

    return jsonify(resultados)

@app.route("/registrar-lote", methods=["POST"])
def registrar_lote():
    data_in = request.get_json(silent=True) or {}
    cpfs = data_in.get("cpfs", [])
    lote_id = data_in.get("lote_id")

    if not isinstance(cpfs, list) or not cpfs:
        return jsonify({"erro": "Lista de CPFs inválida"}), 400

    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for cpf in cpfs:
        c.execute("""
            INSERT OR IGNORE INTO consultas (cpf, resultado, data, lote_id)
            VALUES (?, ?, ?, ?)
        """, (cpf, "Pendente", ts_now, lote_id))
    conn.commit()
    conn.close()

    return jsonify({"msg": f"{len(cpfs)} CPFs registrados no lote {lote_id}."})

@app.route("/baixar-excel", methods=["POST"])
def baixar_excel():
    data_in = request.get_json(silent=True) or {}
    resultados = data_in.get("resultados", [])

    if not resultados:
        return jsonify({"erro": "Sem resultados para exportar"}), 400

    df = pd.DataFrame(resultados)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Consultas")

    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="resultado_consulta.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

if __name__ == "__main__":
    app.run(debug=True, port=8800)
