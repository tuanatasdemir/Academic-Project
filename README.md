# Akademik Kaynakça Doğrulama, Metin İçi Atıf Tutarlılığı ve Çoklu Veri Tabanı Tarama Sistemi

> **Lisans Bitirme Projesi**  
> **Üniversite / Bölüm:** Tekirdağ Namık Kemal Üniversitesi — Bilgisayar Mühendisliği  
> **Geliştirici:** Nimet Tuana Taşdemir 
> **Danışman:** Rabia Korkmaz Tan
> **Akademik Yıl:** [2026-2027]  
> **Sürüm:** 2.0.0

---

## 1. Özet

Bu proje, akademik metinlerdeki (`.docx` ve `.pdf`) kaynakça kayıtlarının doğruluğunu ve metin içi atıflarla olan tutarlılığını otomatik olarak denetleyen, aynı zamanda makale başlığı ve yazar bilgisiyle 7 küresel akademik veri tabanında doğrudan tarama yapabilen web tabanlı bir doğrulama sistemidir.

Sistem; numaralı (`[1]`) ve APA (`(Yazar, Yıl)`) atıf biçimlerini ayrıştırır, metin içi atıf–kaynakça eşleşmelerini kontrol eder ve her bir kaynağı DOI, başlık benzerliği, yazar soyadı, yayın yılı ve sayfa aralığı ölçütleri üzerinden asenkron olarak doğrular. Böylece hatalı/kırık DOI'ler, yazar veya yıl tutarsızlıkları ve üretken yapay zekâ kaynaklı var olmayan (halüsinasyon) referanslar hızlı biçimde tespit edilir.

## 2. Projenin Amacı ve Çözdüğü Problemler

Akademik tez, makale ve bildirilerin hazırlanma sürecinde sık karşılaşılan şu sorunların otomatik denetimi hedeflenmiştir:

- **Metin içi atıf ve kaynakça uyumsuzlukları:** Metinde atıf yapıldığı hâlde kaynakçada yer almayan eserler, kaynakçada bulunup metinde hiç kullanılmayan referanslar ve mükerrer (tekrar eden) kayıtlar.
- **Metadata hataları:** Yazar soyadı, yayın yılı, başlık veya sayfa numarası uyuşmazlıkları.
- **Geçersiz veya sahte DOI kayıtları:** Hatalı yazılmış DOI'lerin tespiti ve eserin başlık/yazar üzerinden gerçek DOI'sinin kurtarılması.
- **DOI'siz yayınların doğrulanması:** Hakemli dergi, konferans bildirisi ve önbasım (preprint) yayınlarının çoklu akademik dizinlerde eşzamanlı sorgulanması.
- **Hızlı tekil literatür taraması:** Dosya yüklemeye gerek kalmadan makale adı ve opsiyonel yazar bilgisiyle doğrudan kaynak doğrulama ve erişim bağlantısına ulaşma.

## 3. Sistem Özellikleri

- **Çoklu Belge Formatı Desteği (`.docx` ve `.pdf`):**
  - Word belgelerinde paragrafların yanı sıra tablo hücrelerindeki atıflar da belge sırası korunarak okunur (`python-docx`).
  - PDF belgelerinde çift sütunlu (IEEE/ACM) düzenler okuma sırasına göre ayrıştırılır; sayfa üst/alt bilgisi, tekil sayfa numaraları, satır sonu tirelemeleri ve Unicode ligatürleri temizlenir (`PyMuPDF / fitz`).
- **Metin İçi Atıf Tutarlılığı Analizi:**
  - Numaralı (`[1]`) ve APA (`(Yazar, Yıl)`) atıf yakalama.
  - Kaynakçada eksik olanlar, metinde hiç kullanılmayanlar, mükerrer kayıtlar ve metin içi yazar/yıl uyuşmazlıkları raporlama.
  - Atıf yapılan cümleler ile makale başlığı/özeti arasındaki kavramsal bağlam kontrolü.
- **7 Küresel Akademik Veri Tabanı Entegrasyonu:**
  - **Katman 1 (Doğrudan DOI Doğrulama):** Crossref, DataCite, OpenAlex ve Europe PMC.
  - **Katman 2 (Başlık + Yazar + Yıl Taraması):** Crossref, OpenAlex, DBLP, Europe PMC; güçlü eşleşme bulunamadığında Semantic Scholar ve arXiv devreye alınır.
  - **Katman 3 (Web / Dokümantasyon Sınıflandırması):** DOI içermeyen ancak geçerli erişim adresi (URL) barındıran yazılım ve teknik dokümantasyon kaynakları ayrı sınıflandırılır.
- **Esnek Metin Benzerliği ve Güven Skoru:**
  - Türkçe karakter ve noktalama normalizasyonu sonrası `RapidFuzz` (`TheFuzz` ve standart `difflib` yedekli) ile başlık benzerliği hesaplanır.
  - Güven skoru ağırlıklı olarak hesaplanır: `%55 Başlık Benzerliği + %30 Yazar Uyumu + %15 Yıl Uyumu` (±1 yıl Online First toleransı ile).
- **İsimle Makale Arama:**
  - Dosya yüklemeden doğrudan başlık ve opsiyonel yazar soyadı ile arama yapılabilir; sonuçlar benzerlik oranına göre sıralanır ve aynı DOI/başlığa sahip tekrarlar temizlenir.
- **Performans ve Dayanıklılık:**
  - Asenkron paralel sorgulama (`asyncio.gather`), eşzamanlılık sınırı (`asyncio.Semaphore(6)`), bellek içi önbellek (`METADATA_CACHE`) ve HTTP 429/ağ hatalarına karşı üstel geri çekilme (`Exponential Backoff`).

## 4. Sistem Mimarisi ve Dizin Yapısı

```text
Academic-Project/
├── main.py                          # FastAPI uygulaması, PDF/DOCX okuma, /analyze ve /search uç noktaları
├── index.html                       # Tailwind CSS tabanlı tek sayfalık web arayüzü
├── requirements.txt                 # Python bağımlılık listesi
├── README.md                        # Proje dokümantasyonu
└── app/
    ├── parsers/
    │   └── citation_regex.py        # Paragraf/PDF çıkarıcı, bölüm ayrıştırıcı, numaralı ve APA atıf denetleyici
    └── services/
        ├── crossref_service.py      # 7 veri tabanlı ana doğrulama, benzerlik motoru ve arama servisi
        ├── arxiv_service.py         # arXiv Atom XML ayrıştırıcı ve arama modülü
        ├── dblp_service.py          # DBLP Computer Science API modülü
        └── europepmc_service.py     # Europe PMC REST API modülü
```

## 5. Kullanılan Teknolojiler ve Veri Kaynakları

| Katman / Alan | Teknoloji / Servis | Kullanım Amacı |
| :--- | :--- | :--- |
| Backend Çatısı | FastAPI, Uvicorn | Asenkron REST API ve statik arayüz sunumu |
| Belge İşleme | `python-docx`, `PyMuPDF (fitz)` | `.docx` ve `.pdf` metin/tablo ayrıştırma |
| Benzerlik Motoru | `rapidfuzz`, `thefuzz`, `python-Levenshtein` | Başlık ve yazar bulanık metin eşleştirme |
| HTTP İstemcisi | `httpx` (AsyncClient) | Harici akademik API'lere paralel ve yeniden denemeli istekler |
| Frontend Arayüzü | HTML5, Tailwind CSS (CDN), Vanilla JavaScript | Dosya yükleme, isimle arama ve sonuç görselleştirme |
| Akademik Veri Tabanları | Crossref, OpenAlex, DBLP, Europe PMC, arXiv, DataCite, Semantic Scholar | Çoklu kaynaklı bibliyografik doğrulama ve makale tarama |

## 6. Kurulum ve Çalıştırma

### Gereksinimler

- Python 3.10 veya üzeri
- `pip` paket yöneticisi

### Yerel Kurulum Adımları

1. Depoyu bilgisayarınıza klonlayın ve proje klasörüne girin:
   ```bash
   git clone https://github.com/tuanatasdemir/academic-project.git
   cd academic-project
   ```

2. (Önerilen) Sanal ortam oluşturun ve aktif edin:
   ```bash
   python -m venv venv
   ```
   - **Windows (CMD):**
     ```cmd
     venv\Scripts\activate
     ```
   - **macOS / Linux:**
     ```bash
     source venv/bin/activate
     ```

3. Bağımlılıkları kurun:
   ```bash
   python -m pip install -r requirements.txt
   ```

4. FastAPI sunucusunu başlatın:
   ```bash
   python -m uvicorn main:app --reload
   ```

5. Tarayıcınızdan uygulamaya erişin:
   - **Web Arayüzü:** `http://127.0.0.1:8000`
   - **Etkileşimli API Dokümantasyonu (Swagger UI):** `http://127.0.0.1:8000/docs`

## 7. API Uç Noktaları (Endpoints)

| Metot | Uç Nokta | Açıklama | Temel Parametreler |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | Web arayüzünü (`index.html`) sunar. | — |
| `GET` | `/status` | Sistem durumunu, sürümü ve desteklenen veri tabanlarını döndürür. | — |
| `POST` | `/analyze` | Yüklenen `.docx` veya `.pdf` belgesindeki atıf ve kaynakçaları denetler. | `file` (multipart), `similarity_threshold` (50–100, varsayılan: 80) |
| `GET` | `/search` | Makale başlığı ve opsiyonel yazar/yıl bilgisiyle akademik veri tabanlarında arama yapar. | `query` (en az 6 karakter), `author` (opsiyonel), `year` (opsiyonel) |

### Örnek `/search` İsteği

```http
GET /search?query=Attention+Is+All+You+Need&author=Vaswani
```

## 8. Doğrulama Durum Kodları

| Durum Kodu | Anlamı |
| :--- | :--- |
| `VERIFIED` | DOI üzerinden tam doğrulama başarılı; başlık, yazar ve yıl bilgileri uyumlu. |
| `VERIFIED_BY_TITLE_AUTHOR` | Belgede DOI yer almıyor ancak eser başlık, yazar ve yıl üzerinden doğrulandı. |
| `WEB_OR_DOCUMENTATION_SOURCE` | Akademik DOI yerine erişim adresi içeren web veya yazılım dokümantasyonu kaynağı. |
| `METADATA_MISMATCH` | Başlık eşleşti ancak yazar, yıl veya sayfa bilgisinde uyuşmazlık tespit edildi. |
| `TITLE_MISMATCH_WARNING` | Eser bulundu ancak başlık benzerliği belirlenen eşik değerin altında kaldı. |
| `INVALID_DOI_BUT_PAPER_EXISTS` | Belgedeki DOI geçersiz ancak eser başlık ve yazar taramasıyla bulundu. |
| `FAKE_OR_NOT_FOUND` | Yazılan DOI hiçbir dizinde bulunamadı ve başlık/yazar taraması sonuçsuz kaldı. |
| `UNVERIFIED_NO_MATCH` | DOI'siz kayıt için 7 akademik veri tabanında eşleşen hakemli yayın bulunamadı. |

## 9. Bilinen Sınırlamalar

- Yalnızca metin katmanı içeren PDF dosyaları doğrudan okunabilir; taranmış görüntü (OCR'sız) PDF belgelerinden metin çıkarılamaz.
- Harici akademik API'lerin hız limitleri (rate limit) yoğun sorgularda yanıt süresini uzatabilir.
- Bellek içi önbellek (`METADATA_CACHE`) uygulama yeniden başlatıldığında sıfırlanır.

## 10. Lisans

Bu proje akademik eğitim ve lisans bitirme tezi kapsamında geliştirilmiştir.
