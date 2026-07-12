Flask API που: 
1. Συνδέεται με μια βάση δεδομένων MySQL. 
2. Χρησιμοποιεί Redis ως cache layer, ώστε τα ακριβά queries να μην χτυπάνε τη βάση σε κάθε request. 
3. Τρέχει ολόκληρο μέσα σε Docker (API + βάση + cache — 3 containers που μιλάνε μεταξύ τους μέσω docker compose)