"""Renderiza documento offline e verifica integridade, layout e simulador."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

BASE = Path(__file__).resolve().parent
QA = BASE / 'verificacao'
QA.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto((BASE/'SecretarIA_Dossie.html').as_uri())
    page.wait_for_load_state('load')
    page.emulate_media(media='print')
    overflow = page.evaluate('''() => [...document.querySelectorAll('.sheet')].map(el => {
      const box=el.getBoundingClientRect(); const footer=el.querySelector('.foot').getBoundingClientRect();
      const children=[...el.children].filter(x=>!x.classList.contains('foot')&&!x.classList.contains('orb'));
      const bottom=Math.max(...children.map(x=>x.getBoundingClientRect().bottom));
      return {id:el.id,scroll:el.scrollHeight,client:el.clientHeight,bottomToFooter:Math.round(footer.top-bottom),
              horizontal:el.scrollWidth>el.clientWidth+1};
    })''')
    page.pdf(path=str(BASE/'SecretarIA_Dossie.pdf'), format='A4', print_background=True,
             prefer_css_page_size=True, outline=True, tagged=True)
    # Casos úteis do simulador: central, sem IA, mais volume, faixa longa, entrada inválida.
    central = page.locator('#unitCost').inner_text()
    assert '0,01251' in central, central
    assert '16,27' in page.locator('#monthlyCost').inner_text()
    page.locator('#activation').fill('0')
    assert '0,00000' in page.locator('#unitCost').inner_text()
    page.evaluate('resetSimulation()')
    page.locator('#bookings').fill('10000')
    assert '162,66' in page.locator('#monthlyCost').inner_text()
    page.evaluate('resetSimulation()')
    page.locator('#inputTokens').fill('100100')
    assert 'Faixa maior' in page.locator('#tierNote').inner_text()
    page.locator('#conversion').fill('0')
    assert 'Rever' in page.locator('#unitCost').inner_text()
    page.evaluate('resetSimulation()')
    assert '0,01251' in page.locator('#unitCost').inner_text()
    page.emulate_media(media='screen')
    page.locator('#inicio').screenshot(path=str(QA/'capa.png'))
    page.locator('#arquitetura').screenshot(path=str(QA/'arquitetura.png'))
    page.locator('#simulador').screenshot(path=str(QA/'simulador.png'))
    page.set_viewport_size({'width':390,'height':844})
    page.goto((BASE/'SecretarIA_Dossie.html').as_uri()+'#simulador')
    mobile = page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
    reader = PdfReader(BASE/'SecretarIA_Dossie.pdf')
    pages = [len(page.extract_text() or '') for page in reader.pages]
    assert len(pages)==18, pages
    assert min(pages)>500, pages
    assert not errors, errors
    # Margem mínima entre conteúdo e rodapé para evitar sobreposição.
    bad=[x for x in overflow if x['bottomToFooter']<6 or x['horizontal'] or x['scroll']>x['client']+2]
    result={'pdf_pages':len(pages),'text_chars_per_page':pages,'layout':overflow,'overflow_failures':bad,
            'javascript_errors':errors,'simulator_cases':6,'mobile':mobile}
    (QA/'validacao.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    browser.close()
    print(json.dumps(result,ensure_ascii=False))
    if bad or mobile['scroll']>mobile['width']:
        raise SystemExit(1)
