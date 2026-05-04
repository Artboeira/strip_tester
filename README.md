# LED Strip Tester

Ferramenta de comissionamento e debug de fitas de LED RGB via protocolo **Art-Net / DMX**.  
Desenvolvida no Estúdio AB como utilitário interno.

---

## Pré-requisitos

| Requisito | Versão mínima |
|-----------|---------------|
| Python | 3.10 |
| pip | qualquer |

> Sem dependências de sistema além do Python. Funciona em macOS, Linux e Windows.

---

## Instalação

```bash
# 1. clonar o repositório
git clone <url-do-repo>
cd led-strip-tester

# 2. instalar dependência Python
pip install -r requirements.txt

# 3. executar
python led_tester.py
```

O browser abre automaticamente em `http://localhost:8080`.

### Estrutura do repositório

```
led_tester.py          ← aplicação completa (servidor + interface)
fonts/                 ← fontes Neue Haas Grotesk Display + Calling Code
  CallingCode-Regular.otf
  CallingCode-Bold.ttf
  NeueHaasGrotDisp-55Roman-Trial.otf
  NeueHaasGrotDisp-65Medium-Trial.otf
  NeueHaasGrotDisp-75Bold-Trial.otf
  NeueHaasGrotDisp-95Black-Trial.otf
requirements.txt
README.md
```

As fontes são servidas localmente pelo próprio servidor em `/fonts/`. Se a pasta não existir, a interface carrega normalmente com fontes do sistema como fallback.

### Opções de linha de comando

```bash
python led_tester.py --port 9000          # porta customizada (padrão: 8080)
python led_tester.py --no-browser         # não abre o browser automaticamente
python led_tester.py --fonts-dir /caminho/para/fontes   # fontes em outro diretório
```

---

## Funcionalidades

### Configuração de fitas

Cada fita tem controle independente de:

- **Nome** — identificação visual
- **Pixels** — quantidade de LEDs RGB
- **Universe** (`U`) — universo Art-Net de início (0–32767)
- **Canal** (`ch`) — canal DMX de início dentro do universo (1–512)

Isso permite mapear fitas em nós Art-Net separados, iniciar em canais arbitrários, e reutilizar universos com múltiplas fitas em zonas diferentes de uma instalação.

### Universe Map

Calculado automaticamente após Apply. Mostra exatamente qual universo e quais canais cada fita ocupa, incluindo pixels que cruzam fronteira de universo (512 não é divisível por 3).

Exemplo — Strip 1: 200px em U0/ch1, Strip 2: 60px em U1/ch1:

```
U0 → Strip 1   ch1–512
U1 → Strip 1   ch1–88
     Strip 2   ch1–180
```

### Efeitos (10)

| Efeito | Descrição |
|--------|-----------|
| Solid | Cor sólida |
| Breathing | Fade senoidal in/out |
| Rainbow | Arco-íris estático na fita |
| Rainbow Wave | Arco-íris com movimento |
| Chase | Pixel correndo com cauda fade |
| Theater | Chase estilo teatro (a cada 3px) |
| Fire | Simulação de fogo (heatmap) |
| Twinkle | Pixels piscam aleatoriamente |
| Strobe | Flash estroboscópico |
| Color Wipe | Varre cor pela fita e apaga |

Todos os efeitos respeitam **Brightness** e **Speed**.

### Monitoramento ao vivo

- **Strip Preview** — canvas com cor real de cada pixel, atualizado a 10 fps
- **DMX Monitor** — valores de canal por universo via WebSocket

---

## API REST

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/api/config` | Config atual (IP, fitas, universe map) |
| `POST` | `/api/config` | Atualiza IP e lista de fitas |
| `POST` | `/api/effect` | Muda efeito, cor, brightness, speed |
| `GET` | `/fonts/{nome}` | Serve arquivos de fonte locais |
| `WS` | `/ws` | Push de estado a cada 100 ms |

### Exemplos curl

```bash
# Configurar 2 fitas com universos independentes
curl -X POST http://localhost:8080/api/config \
  -H 'Content-Type: application/json' \
  -d '{
    "ip": "2.0.0.1",
    "strips": [
      {"name": "Fita 1", "pixels": 150, "universe_offset": 0, "start_channel": 1},
      {"name": "Fita 2", "pixels":  60, "universe_offset": 2, "start_channel": 1}
    ]
  }'

# Ativar efeito Fire
curl -X POST http://localhost:8080/api/effect \
  -H 'Content-Type: application/json' \
  -d '{"effect": "fire", "color": [255, 40, 0], "brightness": 0.9, "speed": 0.6}'
```

### Payload WebSocket

```json
{
  "universes": {
    "0": [255, 0, 0, 0, 255, 0, "..."],
    "2": [128, 64, 0, "..."]
  },
  "strips": [
    {"name": "Fita 1", "pixels": [255, 0, 0, 0, 255, 0, "..."]},
    {"name": "Fita 2", "pixels": [128, 64, 0, "..."]}
  ]
}
```

`pixels` é uma lista plana `[r, g, b, r, g, b, ...]`, downsampled a 300px máximo por fita.

---

## Protocolo Art-Net

Pacote ArtDMX enviado via UDP (porta 6454):

```
Bytes  0–7:   "Art-Net\0"    ID
Bytes  8–9:   0x00 0x50      OpCode ArtDMX (little-endian)
Bytes 10–11:  0x00 0x0E      ProtVer 14 (big-endian)
Byte  12:     0x00           Sequence (desabilitado)
Byte  13:     0x00           Physical
Bytes 14–15:  universe       15-bit, little-endian
Bytes 16–17:  length         big-endian, sempre par
Bytes 18+:    dados DMX      até 512 bytes, completados a 512 com zeros
```

Cada universo que contém dados de ao menos uma fita recebe um pacote completo de 512 bytes. Universos sem dados não são enviados.

---

## Design

Interface construída sobre o design system do **Estúdio AB**:

- **Paleta** — ink `#222223`, bone `#EDE5D3`, sage `#B7BAAF`, bark `#453B32`, amber `#EEA244`
- **Tipografia** — Neue Haas Grotesk Display (headers e botões) + Calling Code (corpo, labels e monitor DMX)
- **Gradiente signature** — composição de 4 hot-spots radiais (plum, amber, moss, steel) sobre base sage, conforme brandbook
- **Cantos quadrados** — identidade visual do estúdio; 2px apenas em inputs

> As fontes incluídas na pasta `fonts/` são versões trial (Neue Haas Grotesk) e de uso interno (Calling Code). Para distribuição pública, substitua por licenças comerciais adequadas.
