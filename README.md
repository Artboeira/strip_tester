# LED Strip Tester

Ferramenta de teste para fitas de LED RGB via protocolo **Art-Net / DMX** — arquivo Python único, sem instalação além do `aiohttp`.

Desenvolvida no Estúdio AB como utilitário interno para comissionamento e debug de instalações com fitas LED endereçáveis.

---

## Instalação

```bash
pip install aiohttp
python led_tester.py
```

O browser abre automaticamente em `http://localhost:8080`.

```bash
# Opções disponíveis
python led_tester.py --port 9000      # porta customizada
python led_tester.py --no-browser     # não abre o browser
```

---

## Funcionalidades

### Configuração
- **IP do nó Art-Net** e porta (padrão 6454)
- Múltiplas fitas, nome e quantidade de pixels por fita
- **Universe Map** calculado automaticamente: mostra exatamente qual universo Art-Net e quais canais DMX cada fita ocupa

### Mapeamento DMX
As fitas são empacotadas continuamente no espaço de canais global. Cada universo Art-Net tem 512 canais; com RGB (3 ch/pixel) cabem até 170 pixels por universo. Um pixel pode cruzar a fronteira de universo — o mapa reflete os canais brutos.

Exemplo com Strip 1 = 200px, Strip 2 = 60px:
```
Strip 1 → U0: ch1–512  (170px + início da 171ª)
           U1: ch1–88
Strip 2 → U1: ch89–268
```

### Efeitos (10)
| Efeito | Descrição |
|--------|-----------|
| Solid | Cor sólida |
| Breathing | Fade senoidal in/out |
| Rainbow | Arco-íris estático distribuído na fita |
| Rainbow Wave | Arco-íris com movimento |
| Chase | Pixel correndo com cauda com fade |
| Theater | Chase estilo teatro (a cada 3 pixels) |
| Fire | Simulação de fogo (heatmap) |
| Twinkle | Pixels piscam aleatoriamente |
| Strobe | Flash stroboscópico |
| Color Wipe | Varre cor pela fita e apaga |

Todos os efeitos respeitam os controles de **Brightness** e **Speed**.

### Monitoramento
- **Strip Preview**: canvas ao vivo com visualização dos pixels em tempo real
- **DMX Monitor**: valores de cada canal por universo, atualizado via WebSocket a 10 fps

---

## API REST

A interface comunica com o servidor Python via HTTP + WebSocket local.

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/api/config` | Config atual (IP, fitas, universe map) |
| `POST` | `/api/config` | Atualiza IP e lista de fitas |
| `POST` | `/api/effect` | Muda efeito, cor, brightness, speed |
| `WS` | `/ws` | Push de status a cada 100ms |

### Exemplos curl

```bash
# Configurar 2 fitas
curl -X POST http://localhost:8080/api/config \
  -H 'Content-Type: application/json' \
  -d '{"ip":"2.0.0.1","strips":[{"name":"Fita 1","pixels":150},{"name":"Fita 2","pixels":60}]}'

# Ativar efeito Fire vermelho
curl -X POST http://localhost:8080/api/effect \
  -H 'Content-Type: application/json' \
  -d '{"effect":"fire","color":[255,40,0],"brightness":0.9,"speed":0.6}'
```

### Payload WebSocket

```json
{
  "universes": {
    "0": [255, 0, 0, 0, 255, 0, ...],
    "1": [128, 64, 0, ...]
  },
  "strips": [
    { "name": "Fita 1", "pixels": [255,0,0, 0,255,0, ...] }
  ]
}
```

`pixels` é uma lista plana `[r,g,b, r,g,b, ...]` downsampled a no máximo 300 pixels por fita para eficiência.

---

## Protocolo Art-Net

O pacote ArtDMX enviado via UDP:

```
Bytes 0–7:   "Art-Net\0"         (ID)
Bytes 8–9:   0x00 0x50           (OpCode ArtDMX, little-endian)
Bytes 10–11: 0x00 0x0E           (ProtVer 14, big-endian)
Byte 12:     0x00                (Sequence, desabilitado)
Byte 13:     0x00                (Physical)
Bytes 14–15: universe            (little-endian, 15-bit)
Bytes 16–17: length              (big-endian, sempre par)
Bytes 18+:   dados DMX           (até 512 bytes)
```

---

## Design

Interface visual baseada no brandbook do **Estúdio AB**:
- Paleta terrosa: creme `#EDE5D3`, sage `#B7BAAF`, marrom `#453B32`, âmbar `#EEA244`
- Tipografia: **DM Sans** (interface) + **JetBrains Mono** (valores DMX)
- Linha de gradiente no header usando a paleta completa da marca
