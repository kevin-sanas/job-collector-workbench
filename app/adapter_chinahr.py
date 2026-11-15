"""The public ChinaHR site currently presents itself as Xinhua Yingcai."""
from urllib.parse import parse_qs, urlsplit

from app.adapters import CitySelectionError, DomAdapter


class ChinaHRAdapter(DomAdapter):
    def validate_search_selection(self, page, keyword, city):
        if city not in self.source.get('cities', {}) or urlsplit(page.url).path != '/job':
            return False
        fields = self._search_evidence(page, {
            'keyword': '.search-left_input',
            'city': '.options-lists_cities .options-city-active, .options-lists_cities .options-active'})
        query = parse_qs(urlsplit(page.url).query)
        option = self.source.get('city_options', {}).get(city)
        if option and ('cityId' in query or 'cityName' in query):
            if query.get('cityId') != [option['code']] or query.get('cityName') != [city]:
                return False
        return (fields.get('city') == [city] and fields.get('loaded') is True
                and (keyword is None or fields.get('keyword') == [keyword]))

    def search(self, session, keyword, city, gate, check_page, delay, interval):
        if city not in self.source.get('cities', {}):
            raise CitySelectionError('请先在浏览器手动选择该城市，再使用“采集当前页”')
        for attempt in range(13):
            check_page()
            if session.page.ele('css:.search-left_input',timeout=0):
                break
            if attempt < 12:
                delay(.5)
        page = session.page
        gate()
        target = next((item for item in page.eles('css:.options-lists_cities li') if item.text.strip()==city),None)
        if target is None:
            raise CitySelectionError('未找到城市筛选项，请人工确认页面结构')
        active = target.run_js('return this.classList.contains("options-city-active") || this.classList.contains("options-active");')
        if not active:
            gate()
            target.click()
            delay(interval)
            check_page()
        gate()
        page.ele('css:.search-left_input',timeout=2).input(keyword,clear=True)
        delay(interval)
        check_page()
        gate()
        page.ele('css:.search-btn',timeout=2).click()
        delay(interval)
        check_page()
        verified = session.page.run_js('''return {keyword:document.querySelector('.search-left_input')?.value,
            cities:Array.from(document.querySelectorAll('.options-lists_cities .options-city-active, .options-lists_cities .options-active')).map(e=>e.innerText.trim())};''') or {}
        if verified.get('keyword') != keyword or verified.get('cities') != [city]:
            raise CitySelectionError('搜索条件未确认，请人工筛选后采集当前页')
