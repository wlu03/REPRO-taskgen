# Webscrapping

This repo is a helper method to extract all of the records from World Bank. The process is: 
1. Discover records using GET API method
2. Label the method using a reference
3. Fetch each record through the websites unique IQ
4. Save the original response under `data/`
5. Build one normalized entry per record in `catalog.json`
6. Download the ZIPs, PDFs, etc. 