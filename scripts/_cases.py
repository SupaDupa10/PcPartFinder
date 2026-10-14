"""Echte Titel mit erwartetem Urteil - Grundlage der Messung und der Tests."""

from __future__ import annotations

# (Titel, erwartete Angebotsart oder None = muss durchgelassen werden)
MUST_REJECT: list[tuple[str, str]] = [
    # Suchgesuche / Tausch
    ("Suche vollfunktionsfähige RTX 2060 SUPER für 50€", "suchgesuch"),
    ("SUCHE RTX 2070", "suchgesuch"),
    ("Ich suche rtx 4070", "suchgesuch"),
    ("Suche RTX 3080 Founders Edition, FE, 10Gb oder 12Gb, TI", "suchgesuch"),
    ("SUCHE: Nvidia GeForce RTX 4070 Ti (in NRW)", "suchgesuch"),
    ("Arez Strix Vega 56 Grafikkarte Tausch gegen RTX 3050/2060", "teil"),
    # Verpackung
    ("EVGA GeForce RTX 2060 XC Gaming 12GB Originalverpackung OVP Grafikkarte GPU", "verpackung"),
    ("KARTON ASUS GeForce RTX 2060 6GB GDDR6 Grafikkarte (DUAL-RTX2060-O6G-EVO)", "verpackung"),
    ('ASUS RTX 2060 OC Edition 6GB Dual-Ball "BOX ONLY NO GRAPHICS CARD BOX ONLY"', "verpackung"),
    ("AMD rx 6800 XT OVP Kartons zwei stück", "verpackung"),
    ("Asus Prime GeForce RTX5060ti 16gb Karton", "verpackung"),
    # Teile und Zubehör
    ("NUR KÜHLER!! MSI RTX 3060 GAMING X 12G", "teil"),
    ("AMD Sapphire RX 5700(XT) Lüfter (Non RGB)", "teil"),
    ("AMD RX6800XT Reference Cooler", "teil"),
    ("EVGA Trim Kit RTX 2060 2070 2080 Ti Dual Fan Karten ROT Zubehör", "teil"),
    ("Bykski Full Coverage GPU Water Block nVidia Founders N-RTX2070XV4", "teil"),
    ("Heat sink thermal module For Gigabyte G5 KF KF5 G5 RTX4060 6-31-NP5K2-102 *hu", "teil"),
    ("Kupfer-Speicherkühler Copper-Mod für Nvidia ASUS RogStrix RTX 3070 Ti u.a.", "teil"),
    ("NVIDIA NVLink Bridge 2-Slot 900-53651 P3651 for RTX 3090 A5000 A5500 A6000", "teil"),
    ("ASUS ROG NVLink Bridge 4 Slot für NVIDIA GeForce RTX 2080ti", "teil"),
    ("EVGA PowerLink Grafikkarte Stromadapter RTX 2080 Ti GTX 1080 Ti", "teil"),
    # Fremdgeräte
    ("180W Netzteil für Razer Blade 15 Basismodell bestückt mit einer GTX 1660Ti", "fremdgeraet"),
    ("230W Netzteil für Acer Nitro 5 N22C1 AN515-58-725A RTX3060", "fremdgeraet"),
    ("Original 240W Netzteil für Dell Alienware x17 R2 RTX3060", "fremdgeraet"),
    ("330W Netzteil für MSI Trident 3 Gaming PC RTX 3060", "fremdgeraet"),
    ("For ROG Zephyrus G14 (2024) GA403 GA403UI RTX4070 ADP-200JB K 20V 10A 200W *cc", "fremdgeraet"),
    ("Lenovo Legion 5 Pro 16ITH6H RTX 3060 Heatsink Dual Fan Assembly 5H40S20371", "fremdgeraet"),
    ("original Cooler HP Omen 17-CB i7-9750h GTX 1660Ti / 17-cb0247ng Kühlkörper", "fremdgeraet"),
    ("acer predator helios neo 16 Core Ultra 9 32 GB Ram RTX 5090 Gaming Laptop", "fremdgeraet"),
    ("HP OMEN 17 | RTX 4090 16GB | i7-13700HX | 32GB | 1TB | QHD 240Hz", "fremdgeraet"),
    ("RTX5060 i7 14650HX 24GB 1TB HP OMEN 15-ga0770ng Gaming Laptop", "fremdgeraet"),
    ("USB4 Thunderbolt 3/4 eGPU Dock Station Suport RTX4090 / RX7900XT Graphics Card", "fremdgeraet"),
    ("AB463-60006 HP RX3600 RX6600 8-Slot SAS Backplane Board ***Ausgebaut***", "fremdgeraet"),
    ("AD127A HP Integrity RX6600 Server 48-Slot Speicher Erweiterung Brett AD127-2100D", "fremdgeraet"),
    ("HP AB463–67004 Core I/O Upgrade für RX3600 RX6600", "fremdgeraet"),
    # Sammelanzeige
    ("Grafikkarten: RTX 3080Ti 2060 GTX1080 1060 970 RX6500XT RX580 uvm", "sammelanzeige"),
]

# Diese Titel sind echte Karten und müssen durchgelassen werden.
MUST_ACCEPT: list[str] = [
    "Gigabyte AORUS GeForce GTX 1080ti Waterforce Extreme 11GB",
    "Geforce gtx 1080ti Zotac",
    "ASUS ROG STRIX GEFORCE GTX 1080TI-011G",
    "GTX 1660 6g OC",
    "RTX 2080super Palit GameRock 8gb VRAM",
    "ASUS Geforce RTX 3050 OC 6GB fast neu Gaming Grafikkarte",
    "RTX 3060 ti 8GB",
    "ASUS Dual GeForce RTX 4070 EVO OC Edition 12 GB GDDR6X",
    "asus rtx 4070 dual 12gb",
    "MSI GeForce GTX 1080 Ti Gaming X Trio 11GB GDDR5X",
    "ASUS TUF Gaming GeForce RTX 3070 OC 8GB GDDR6 Dual Fan",
    # "Single-Lüfter" und "Dual Fan" sind Ausstattung, kein Teil.
    "ZOTAC NVIDIA GeForce GTX 1660 Super PCIe Grafikkarte HDMI Single-Lüfter",
    "ASUS GeForce GTX 1660 Ti Dual Fan 6GB GDDR6",
    # "OVP" neben echten Hardware-Angaben schließt nicht aus.
    "Gigabyte GeForce GTX 1660 Super OC 6GB GDDR6 Grafikkarte – inkl. OVP",
    "MSI GeForce RTX 3060 Gaming X 12G DDR6 OVP",
    "EVGA GeForce RTX 3060 XC Gaming 12GB GDDR6 Grafikkarte mit OVP",
    "Sapphire Pulse Radeon RX 6600 8GB GDDR6 Grafikkarte",
    "NVIDIA GeForce RTX 3080 Founders Edition 10GB GDDR6X",
    "Gigabyte GeForce RTX 3060 Gaming OC 12G, 3x Lüfter, OVP vorhanden",
]
