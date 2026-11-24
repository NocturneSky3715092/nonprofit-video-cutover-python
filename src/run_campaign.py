import argparse

from nonprofit_video_service import CampaignRequest, create_campaign_clip


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a nonprofit campaign clip")
    parser.add_argument("kind", choices=["donor_receipt", "volunteer_reminder", "campaign_report"])
    parser.add_argument("audience")
    parser.add_argument("message")
    parser.add_argument("--bucket", default="nonprofit-clips")
    args = parser.parse_args()
    result = create_campaign_clip(CampaignRequest(args.kind, args.audience, args.message), bucket=args.bucket)
    print(f"video_id={result.video_id}\ndownload_url={result.download_url}")


if __name__ == "__main__":
    main()
