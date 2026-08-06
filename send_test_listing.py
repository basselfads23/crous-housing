import os
import sys
from crous_watcher import send_ntfy_notification

def main():
    topic = os.getenv("NTFY_TOPIC")
    if len(sys.argv) > 1:
        topic = sys.argv[1]

    if not topic:
        print("Usage: python send_test_listing.py <YOUR_NTFY_TOPIC>")
        sys.exit(1)

    title = "🏠 CROUS Test Listing: Cité Guérin"
    message = (
        "Logement: Cité Guérin\n"
        "Prix: 266.62 €\n"
        "Surface: 12 m²\n"
        "Adresse: 39A rue Camille Guérin 87038 LIMOGES CEDEX\n"
        "Lien: https://trouverunlogement.lescrous.fr/tools/42/accommodations/285"
    )
    link = "https://trouverunlogement.lescrous.fr/tools/42/accommodations/285"

    print(f"Sending test notification to ntfy.sh/{topic}...")
    success = send_ntfy_notification(
        topic=topic,
        title=title,
        message=message,
        tags="house,euro",
        link=link
    )
    if success:
        print("Success! Check your phone.")
    else:
        print("Failed to send notification.")

if __name__ == "__main__":
    main()
