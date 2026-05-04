# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Projeto

`led_tester.py` — arquivo Python único (~1000 linhas). Servidor web local que envia pacotes Art-Net (UDP) para nós DMX enquanto exibe uma SPA de controle de fitas LED. Criado para o Estúdio AB como ferramenta de comissionamento.

**Dependência única:** `aiohttp` (já instalado no ambiente do Estúdio AB).

```bash
python led_tester.py [--port 8080] [--no-browser]
```

---

## Arquitetura

O arquivo é dividido em seções claramente marcadas com comentários `# ──`:

```
ArtNetSender        — monta e envia pacotes ArtDMX via socket UDP bloqueante
StripManager        — mantém config (IP + lista de fitas), calcula universe map, chama sender
AnimationEngine     — asyncio task a 40fps: calcula pixels por efeito → chama StripManager.send_all()
HTTP handlers       — 4 rotas aiohttp + WebSocket handler
_broadcast_loop()   — asyncio task a 10fps: serializa ws_payload() e envia para todos ws_peers
HTML_TEMPLATE       — string raw com HTML/CSS/JS completo (SPA sem frameworks)
main()              — argparse + web.run_app()
```

**Estado global:** `sm` (StripManager), `engine` (AnimationEngine), `ws_peers` (set de WebSocketResponse). Simples e intencional — é uma ferramenta single-user.

### Fluxo de dados

```
Browser ──POST /api/config──► StripManager.update_config()
Browser ──POST /api/effect──► engine.{effect, color, brightness, speed}

AnimationEngine._run() [40fps]
  └─► _effect_pixels() por strip  →  StripManager.send_all()
        └─► ArtNetSender.send_universe() via UDP  →  nó Art-Net físico
              └─► universe_data salvo em engine._universe_data

_broadcast_loop() [10fps]
  └─► engine.ws_payload()  →  ws_peers (WebSocket broadcast)
        └─► Browser: atualiza canvas preview + DMX monitor
```

### Mapeamento DMX

Fitas são empacotadas linearmente no espaço global de canais (RGB = 3 ch/pixel). `StripManager.get_universe_map()` divide esse espaço em segmentos de 512 canais. Um pixel **pode cruzar** a fronteira de universo (512 não é divisível por 3) — isso é intencional e reflete o comportamento real do Art-Net.

`send_all()` constrói um array flat de canais e itera em chunks de 512 para gerar os pacotes por universo.

---

## Adicionar um novo efeito

1. Em `AnimationEngine._effect_pixels()`, adicionar um bloco `if self.effect == 'nome':` que retorna `list[tuple[int,int,int]]` com `n` pixels.
2. No `HTML_TEMPLATE`, no array `EFFECTS` do JavaScript, adicionar `{id:'nome', icon:'◆', name:'Nome'}`.

Não há mais nada a mudar — o roteamento de efeitos é puramente por string.

---

## HTML_TEMPLATE

É uma raw string Python (`r"""..."""`) contendo HTML/CSS/JS completo. Pontos importantes:

- **Fontes**: Google Fonts `DM Sans` + `JetBrains Mono` via CDN — requer internet no primeiro carregamento (depois fica em cache).
- **WebSocket**: o JS conecta em `/ws` e recebe JSON `{universes, strips}`. `strips[i].pixels` é array flat `[r,g,b,r,g,b,...]` downsampled a 300px máx por eficiência.
- **Sem frameworks**: JS vanilla puro, sem bundler, sem dependências externas no frontend.
- **State**: objeto `S` no JS centraliza todo o estado da UI (strips, ip, effect, color, brightness, speed, activeU).

---

## Paleta de cores (Estúdio AB Brandbook)

Variáveis CSS definidas em `:root` no HTML_TEMPLATE:

| Variável | Hex | Uso |
|----------|-----|-----|
| `--bg` | `#121110` | Fundo principal |
| `--surf` | `#1c1a17` | Header, sidebar, painéis inferiores |
| `--surf2` | `#242119` | Cards de seção |
| `--border` | `#383229` | Bordas |
| `--text` | `#ede5d3` | Texto principal (creme) |
| `--dim` | `#b7baaf` | Texto secundário (sage) |
| `--acc` | `#eea244` | Acento primário (âmbar) |
| `--acc-lt` | `#f5c97a` | Âmbar claro (hover) |
| `--brown` | `#453b32` | Marrom (border-left umap, tabs ativas) |
| `--brick` | `#bf4128` | Tijolo (delete/error) |
| `--olive` | `#89993e` | Oliva |
| `--steel` | `#4b657e` | Azul aço |
| `--wine` | `#8a1d33` | Vinho (início do gradiente do header) |
| `--green` | `#a8b86b` | Status conectado |

O gradiente do header usa: `wine → brown → steel → olive → acc → dim`.

---

## Melhorias futuras

### Funcionalidade
- **Salvar/carregar presets**: serializar `{strips, ip, effect, color, brightness, speed}` em JSON para arquivo local via endpoint `POST /api/preset` + `GET /api/presets`.
- **Suporte RGBW**: adicionar campo `type: 'rgb' | 'rgbw'` por fita. RGBW = 4 ch/pixel, 128px/universo. Requer mudança em `send_all()` e `get_universe_map()`.
- **Multi-segmento por fita**: definir offset de início de universo e canal por fita, em vez de empacotamento linear automático — necessário para instalações com nós Art-Net separados por zona.
- **Sequence number no Art-Net**: incrementar o byte `Sequence` (atualmente 0) para que receptores detectem pacotes fora de ordem.
- **Blackout global**: botão que zera todos os canais e envia universos zerados imediatamente.
- **Efeitos adicionais**: Comet (rastro longo), Palette Cycle, Noise/Perlin, Segmentos com efeitos independentes por fita.
- **Seleção de segmento**: aplicar efeito a um range de pixels dentro de uma fita (início/fim), não apenas à fita inteira.

### Arquitetura
- **Config persistente**: ao iniciar, ler `~/.led_tester.json` com última config e restaurar estado. Salvar automaticamente ao pressionar Apply.
- **Múltiplos universos de origem**: atualmente o offset de universo é sempre 0. Adicionar campo `universe_offset` por fita ou global.
- **Rate limiting do broadcast WS**: atualmente 10fps fixo. Tornar configurável ou adaptar ao FPS real da engine.
- **Separar HTML em arquivo externo**: durante desenvolvimento, útil ter `template.html` em disco e embeddar apenas no build final (usando `python build.py > led_tester.py`).
- **Test de conectividade Art-Net**: endpoint `POST /api/ping` que envia um universo de teste e aguarda resposta ArtPoll para confirmar se o nó está acessível.

### UI / UX
- **Tema claro**: versão com fundo creme `#EDE5D3` e texto `#222223` para uso em ambientes iluminados.
- **Keyboard shortcuts**: `Space` para blackout, `1–9` para ativar efeitos, `B`/`S` para focar sliders.
- **Visualização 2D**: para instalações matriciais (LED panels), mostrar preview em grid 2D além da barra linear.
- **Indicador de FPS real**: mostrar o FPS efetivo da engine no header para diagnosticar lentidão em setups grandes.
- **Input de cor HEX**: campo de texto ao lado do color picker para colar valores hex diretamente.
